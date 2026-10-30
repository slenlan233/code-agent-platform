"""Finite PowerShell commands using the shared isolated process runner."""
import base64

from .runtime import DockerRunner


class PowerShellRunner(DockerRunner):
    def command(self, project, workspace, name):
        command = super().command(project, {**workspace, "read_only": True}, name)
        index = command.index(project["sandbox_image"])
        command[index:index] = ["--env=HOME=/tmp", "--env=POWERSHELL_TELEMETRY_OPTOUT=1", "--env=DOTNET_CLI_TELEMETRY_OPTOUT=1", "--env=GIT_TERMINAL_PROMPT=0", "--env=GCM_INTERACTIVE=Never"]
        return command

    def run(self, task, project, workspace, script, timeout_seconds):
        header = "$ErrorActionPreference='Stop'; $PSNativeCommandUseErrorActionPreference=$true; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        encoded = base64.b64encode((header + script).encode("utf-16-le")).decode("ascii")
        execution = {**project, "sandbox_image": self.settings.powershell_image,"database_test_profile":"none",
                     "test_command": ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]}
        bounded_task = {**task, "budget": {**task["budget"], "max_seconds": min(timeout_seconds, task["budget"]["max_seconds"])}}
        result = self.verify(bounded_task, execution, workspace)
        result.update(kind="powershell", command=["pwsh", "-NoProfile", "-NonInteractive", "-Command", script])
        # This is a command report, never proof that the test suite passed.
        result.pop("test_count", None)
        return result
