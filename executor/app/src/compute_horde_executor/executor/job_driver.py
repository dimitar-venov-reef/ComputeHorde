import asyncio
import logging

import packaging.version
import sentry_sdk
from compute_horde.job_errors import HordeError, JobError
from compute_horde.protocol_consts import (
    HordeFailureReason,
    JobFailureReason,
    JobParticipantType,
    JobStage,
)
from compute_horde.protocol_messages import (
    FailureContext,
    V0HordeFailedRequest,
    V0InitialJobRequest,
    V0JobFailedRequest,
)
from compute_horde.utils import MachineSpecs, Timer
from django.conf import settings

from compute_horde_executor.executor.job_runner import BaseJobRunner
from compute_horde_executor.executor.miner_client import (
    MinerClient,
)
from compute_horde_executor.executor.utils import (
    docker_container_wrapper,
    get_docker_container_outputs,
    get_machine_specs,
)

logger = logging.getLogger(__name__)

CVE_2022_0492_IMAGE = (
    "us-central1-docker.pkg.dev/twistlock-secresearch/public/can-ctr-escape-cve-2022-0492:latest"
)
# Previous CVE: CVE-2024-0132 fixed in 1.16.2
# Current CVE: CVE-2025-23359 fixed in 1.17.4
NVIDIA_CONTAINER_TOOLKIT_MINIMUM_SAFE_VERSION = packaging.version.parse("1.17.4")


class JobDriver:
    """
    NOTE: This used to be the main body of the "run_executor" management command.
    The distinction between JobDriver and JobRunner is not very clear.
    """

    def __init__(self, runner: BaseJobRunner, miner_client: MinerClient, startup_time_limit: int):
        self.runner = runner
        self.miner_client = miner_client
        self.startup_time_limit = startup_time_limit
        self.specs: MachineSpecs | None = None
        self.deadline = Timer()
        self.current_stage = JobStage.UNKNOWN

    @property
    def time_left(self) -> float:
        return self.deadline.time_left()

    async def execute(self):
        async with self.miner_client:  # TODO: Can this hang?
            try:
                await self._execute()

            except JobError as e:
                logger.error(str(e), exc_info=True)
                await self.send_job_failed(e.message, e.reason, e.context)

            except Exception as e:
                sentry_sdk.capture_exception(e)
                e = HordeError.wrap_unhandled(e)
                e.add_context({"stage": self.current_stage})
                logger.exception(str(e), exc_info=True)
                await self.send_horde_failed(e.message, e.reason, e.context)

            finally:
                try:
                    await self.runner.clean()
                except Exception as e:
                    logger.error(f"Job cleanup failed: {e}")

    async def _execute(self):
        # This limit should be enough to receive the initial job request, which contains further timing details.
        self._set_deadline(self.startup_time_limit, "startup time limit")
        try:
            async with asyncio.timeout(self.time_left):
                initial_job_request = await self._startup_stage()
                timing_details = initial_job_request.executor_timing
        except TimeoutError as e:
            raise HordeError("Timed out waiting for initial job details from miner") from e

        if timing_details:
            # With timing details, re-initialize the deadline with leeway
            # It will be extended before each stage down the line
            self._set_deadline(timing_details.allowed_leeway, "allowed leeway")
        elif initial_job_request.timeout_seconds is not None:
            # For single-timeout, this is the full timeout for the whole job
            self._set_deadline(initial_job_request.timeout_seconds, "single-timeout mode")
        else:
            raise HordeError(
                "No timing received: either timeout_seconds or timing_details must be set"
            )

        # Download stage
        if timing_details:
            self._extend_deadline(timing_details.download_time_limit, "download time limit")
        try:
            await asyncio.wait_for(self._download_stage(), self.time_left)
        except TimeoutError as e:
            raise JobError("Download time exceeded", JobFailureReason.TIMEOUT) from e

        # Execution stage
        if timing_details:
            self._extend_deadline(timing_details.execution_time_limit, "execution time limit")
            if self.runner.is_streaming_job:
                self._extend_deadline(
                    timing_details.streaming_start_time_limit, "streaming start time limit"
                )
        try:
            await asyncio.wait_for(self._execution_stage(), self.time_left)
        except TimeoutError as e:
            raise JobError("Execution time exceeded", JobFailureReason.TIMEOUT) from e

        # Upload stage
        if timing_details:
            self._extend_deadline(timing_details.upload_time_limit, "upload time limit")
        try:
            await asyncio.wait_for(self._upload_stage(), self.time_left)
        except TimeoutError as e:
            raise JobError("Upload time exceeded", JobFailureReason.TIMEOUT) from e

        logger.debug(f"Finished with {self.time_left:.2f}s time left")

    def _set_deadline(self, seconds: float, reason: str):
        self.deadline.set_timeout(seconds)
        logger.debug(f"Setting deadline to {seconds}s: {reason}")

    def _extend_deadline(self, seconds: float, reason: str):
        self.deadline.extend_timeout(seconds)
        logger.debug(
            f"Extending deadline by +{seconds:.2f}s to {self.deadline.time_left():.2f}s: {reason}"
        )

    def _enter_stage(self, stage: JobStage) -> None:
        self.current_stage = stage
        logger.debug(
            f"Entering stage {stage.value} with {self.deadline.time_left():.2f}s time left"
        )

    async def _startup_stage(self) -> V0InitialJobRequest:
        self._enter_stage(JobStage.EXECUTOR_STARTUP)
        if not settings.DEBUG_NO_GPU_MODE:
            self.specs = await get_machine_specs()
        await self.run_security_checks_or_fail()
        initial_job_request = await self.miner_client.initial_msg
        await self.runner.prepare_initial(initial_job_request)
        await self.miner_client.send_executor_ready()
        if initial_job_request.streaming_details is not None:
            assert initial_job_request.streaming_details.executor_ip is not None
            self.runner.generate_streaming_certificate(
                executor_ip=initial_job_request.streaming_details.executor_ip,
                public_key=initial_job_request.streaming_details.public_key,
            )
        return initial_job_request

    async def _download_stage(self):
        self._enter_stage(JobStage.VOLUME_DOWNLOAD)
        logger.debug("Waiting for full payload")
        full_job_request = await self.miner_client.full_payload
        logger.debug("Full payload received")
        await self.runner.prepare_full(full_job_request)
        await self.runner.download_volume()
        await self.miner_client.send_volumes_ready()

    async def _execution_stage(self):
        self._enter_stage(JobStage.EXECUTION)
        async with self.runner.start_job():
            if self.runner.is_streaming_job:
                assert self.runner.executor_certificate is not None, (
                    "Executor certificate is missing."
                )
                await self.miner_client.send_streaming_job_ready(self.runner.executor_certificate)
        await self.fail_if_execution_unsuccessful()
        await self.miner_client.send_execution_done()

    async def _upload_stage(self):
        self._enter_stage(JobStage.RESULT_UPLOAD)
        job_result = await self.runner.upload_results()
        job_result.specs = self.specs
        await self.miner_client.send_result(job_result)

    async def run_security_checks_or_fail(self):
        await self.run_cve_2022_0492_check_or_fail()
        if not settings.DEBUG_NO_GPU_MODE:
            await self.run_nvidia_toolkit_version_check_or_fail()

    async def run_cve_2022_0492_check_or_fail(self):
        async with docker_container_wrapper(
            image=CVE_2022_0492_IMAGE, auto_remove=True
        ) as docker_container:
            results = await docker_container.wait()
            return_code = results["StatusCode"]
            stdout, stderr = await get_docker_container_outputs(docker_container)

        if return_code != 0:
            raise HordeError(
                "CVE-2022-0492 check failed",
                reason=HordeFailureReason.SECURITY_CHECK_FAILED,
                context={
                    "return_code": return_code,
                    "stdout": stdout.decode(),
                    "stderr": stderr.decode(),
                },
            )

        expected_output = "Contained: cannot escape via CVE-2022-0492"
        if expected_output not in stdout:
            raise HordeError(
                f'CVE-HordeFailureReason-0492 check failed: "{expected_output}" not in stdout.',
                reason=HordeFailureReason.SECURITY_CHECK_FAILED,
                context={
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )

    async def run_nvidia_toolkit_version_check_or_fail(self):
        async with docker_container_wrapper(
            image="ubuntu:latest",
            command=["bash", "-c", "nvidia-container-toolkit --version"],
            auto_remove=True,
            HostConfig={
                "Privileged": True,
                "Binds": [
                    "/:/host:ro",
                    "/usr/bin:/usr/bin",
                    "/usr/lib:/usr/lib",
                ],
            },
        ) as docker_container:
            results = await docker_container.wait()
            return_code = results["StatusCode"]
            stdout, stderr = await get_docker_container_outputs(docker_container)

        if return_code != 0:
            raise HordeError(
                f"nvidia-container-toolkit check failed: exit code {return_code}",
                reason=HordeFailureReason.SECURITY_CHECK_FAILED,
                context={
                    "return_code": return_code,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )

        lines = stdout.splitlines()
        if not lines:
            raise HordeError(
                "nvidia-container-toolkit check failed: no output from nvidia-container-toolkit",
                reason=HordeFailureReason.SECURITY_CHECK_FAILED,
                context={
                    "return_code": return_code,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )

        version = lines[0].rpartition(" ")[2]
        is_fixed_version = (
            packaging.version.parse(version) >= NVIDIA_CONTAINER_TOOLKIT_MINIMUM_SAFE_VERSION
        )
        if not is_fixed_version:
            raise HordeError(
                f"Outdated NVIDIA Container Toolkit detected:"
                f'{version}" not >= {NVIDIA_CONTAINER_TOOLKIT_MINIMUM_SAFE_VERSION}',
                reason=HordeFailureReason.SECURITY_CHECK_FAILED,
                context={
                    "return_code": return_code,
                    "stdout": stdout.decode(),
                    "stderr": stderr.decode(),
                },
            )

    async def fail_if_execution_unsuccessful(self):
        assert self.runner.execution_result is not None, "No execution result"

        if self.runner.execution_result.timed_out:
            raise JobError(
                "Job container timed out during execution",
                reason=JobFailureReason.TIMEOUT,
            )

        if self.runner.execution_result.return_code != 0:
            raise JobError(
                f"Job container exited with non-zero exit code: {self.runner.execution_result.return_code}",
                reason=JobFailureReason.NONZERO_RETURN_CODE,
            )

    async def send_job_failed(
        self,
        message: str,
        reason: JobFailureReason,
        context: FailureContext | None = None,
    ):
        execution_result = self.runner.execution_result
        await self.miner_client.send_job_failed(
            V0JobFailedRequest(
                job_uuid=self.miner_client.job_uuid,
                stage=self.current_stage,
                reason=reason,
                message=message,
                docker_process_exit_status=execution_result.return_code
                if execution_result
                else None,
                docker_process_stdout=execution_result.stdout if execution_result else None,
                docker_process_stderr=execution_result.stderr if execution_result else None,
                context=context,
            )
        )

    async def send_horde_failed(
        self, message: str, reason: HordeFailureReason, context: FailureContext | None = None
    ):
        await self.miner_client.send_horde_failed(
            V0HordeFailedRequest(
                job_uuid=self.miner_client.job_uuid,
                reported_by=JobParticipantType.EXECUTOR,
                reason=reason,
                message=message,
                context=context,
            )
        )
