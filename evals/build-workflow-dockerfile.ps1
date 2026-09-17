param(
    [Parameter(Mandatory)]
    [string]$SourceDockerfile,
    [Parameter(Mandatory)]
    [string]$Oracle,
    [Parameter(Mandatory)]
    [string]$CapVerifier,
    [Parameter(Mandatory)]
    [string]$LineageVerifier,
    [Parameter(Mandatory)]
    [string]$OutputDockerfile
)

$ErrorActionPreference = "Stop"
$Dockerfile = [IO.File]::ReadAllText($SourceDockerfile)
$Payload = [Convert]::ToBase64String([IO.File]::ReadAllBytes($Oracle))
$VerifierPayload = [Convert]::ToBase64String([IO.File]::ReadAllBytes($CapVerifier))
$LineagePayload = [Convert]::ToBase64String([IO.File]::ReadAllBytes($LineageVerifier))
$Launcher = @'
#define _GNU_SOURCE
#include <fcntl.h>
#include <grp.h>
#include <stddef.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define GRADER_ID 20001

static _Noreturn void deny(void) {
    static const char message[] = "oracle unavailable\n";
    ssize_t result = write(STDERR_FILENO, message, sizeof message - 1);
    (void)result;
    _exit(126);
}

static int valid_case(const char *value) {
    static const char *const cases[] = {
        "durable-roundtrip",
        "conflict-roundtrip",
        "reflection-roundtrip",
        "delegation-roundtrip",
        "selection-roundtrip",
        "tracked-lineage-roundtrip",
        "ephemeral-pipeline"
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; ++i)
        if (strcmp(value, cases[i]) == 0) return 1;
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 2 || getuid() == 0 || getuid() == GRADER_ID ||
        geteuid() != 0 || !valid_case(argv[1])) deny();

    int null_fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
    if (null_fd < 0 || dup2(null_fd, STDIN_FILENO) < 0) deny();
    if (null_fd > STDERR_FILENO) close(null_fd);
    if (close_range(3, ~0U, 0) != 0 || chdir("/app") != 0) deny();

    umask(077);
    if (setgroups(0, NULL) != 0 ||
        setresgid(GRADER_ID, GRADER_ID, GRADER_ID) != 0 ||
        setresuid(GRADER_ID, GRADER_ID, GRADER_ID) != 0 ||
        getuid() != GRADER_ID || geteuid() != GRADER_ID ||
        getgid() != GRADER_ID || getegid() != GRADER_ID ||
        getgroups(0, NULL) != 0 ||
        prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0 ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) deny();

    char *const child_argv[] = {
        "/usr/bin/pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
        "/opt/mycelium-grader/workflow-oracle.ps1", argv[1],
        "-Workspace", "/app", NULL
    };
    char *const child_env[] = {
        "PATH=/usr/bin:/bin",
        "HOME=/var/lib/mycelium-grader",
        "TMPDIR=/var/lib/mycelium-grader/tmp",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "DOTNET_CLI_TELEMETRY_OPTOUT=1",
        "POWERSHELL_TELEMETRY_OPTOUT=1",
        NULL
    };
    execve(child_argv[0], child_argv, child_env);
    deny();
}
'@
$LauncherPayload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Launcher))
$Marker = "# MYCELIUM_WORKFLOW_ORACLE_PAYLOAD"
if (($Dockerfile.Split($Marker).Count - 1) -ne 1) {
    throw "Benchmark Dockerfile must contain one workflow-oracle marker"
}

$InjectionLines = @(
    "RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*",
    "RUN : > /tmp/mycelium-workflow-oracle.b64",
    "RUN : > /tmp/mycelium-cap-verifier.b64",
    "RUN : > /tmp/mycelium-lineage-verifier.b64",
    "RUN : > /tmp/mycelium-workflow-launcher.b64"
)
for ($Offset = 0; $Offset -lt $Payload.Length; $Offset += 32000) {
    $Length = [Math]::Min(32000, $Payload.Length - $Offset)
    $Chunk = $Payload.Substring($Offset, $Length)
    $InjectionLines += "RUN printf '%s' '$Chunk' >> /tmp/mycelium-workflow-oracle.b64"
}
for ($Offset = 0; $Offset -lt $VerifierPayload.Length; $Offset += 32000) {
    $Length = [Math]::Min(32000, $VerifierPayload.Length - $Offset)
    $Chunk = $VerifierPayload.Substring($Offset, $Length)
    $InjectionLines += "RUN printf '%s' '$Chunk' >> /tmp/mycelium-cap-verifier.b64"
}
for ($Offset = 0; $Offset -lt $LineagePayload.Length; $Offset += 32000) {
    $Length = [Math]::Min(32000, $LineagePayload.Length - $Offset)
    $Chunk = $LineagePayload.Substring($Offset, $Length)
    $InjectionLines += "RUN printf '%s' '$Chunk' >> /tmp/mycelium-lineage-verifier.b64"
}
for ($Offset = 0; $Offset -lt $LauncherPayload.Length; $Offset += 32000) {
    $Length = [Math]::Min(32000, $LauncherPayload.Length - $Offset)
    $Chunk = $LauncherPayload.Substring($Offset, $Length)
    $InjectionLines += "RUN printf '%s' '$Chunk' >> /tmp/mycelium-workflow-launcher.b64"
}
$InjectionLines += @(
    "RUN base64 -d /tmp/mycelium-workflow-oracle.b64 > /opt/mycelium-grader/workflow-oracle.ps1 \",
    "    && base64 -d /tmp/mycelium-cap-verifier.b64 > /opt/mycelium-grader/verify-cap.py \",
    "    && base64 -d /tmp/mycelium-lineage-verifier.b64 > /opt/mycelium-grader/mycelium_lineage.py \",
    "    && base64 -d /tmp/mycelium-workflow-launcher.b64 > /tmp/mycelium-workflow-launcher.c \",
    "    && gcc -std=c11 -O2 -static-pie -fstack-protector-strong -D_FORTIFY_SOURCE=3 -Wall -Wextra -Werror -Wl,-z,relro,-z,now /tmp/mycelium-workflow-launcher.c -o /usr/local/bin/mycelium-workflow-oracle \",
    "    && chown root:mycelium-grader /opt/mycelium-grader /opt/mycelium-grader/workflow-oracle.ps1 /opt/mycelium-grader/verify-cap.py /opt/mycelium-grader/mycelium_lineage.py \",
    "    && chmod 0750 /opt/mycelium-grader \",
    "    && chmod 0440 /opt/mycelium-grader/workflow-oracle.ps1 /opt/mycelium-grader/verify-cap.py /opt/mycelium-grader/mycelium_lineage.py \",
    "    && chown -R mycelium-grader:mycelium-grader /var/lib/mycelium-grader \",
    "    && chmod 0700 /var/lib/mycelium-grader /var/lib/mycelium-grader/tmp \",
    "    && chown root:root /usr/local/bin/mycelium-workflow-oracle \",
    "    && chmod 4511 /usr/local/bin/mycelium-workflow-oracle \",
    "    && rm /tmp/mycelium-workflow-oracle.b64 /tmp/mycelium-cap-verifier.b64 /tmp/mycelium-lineage-verifier.b64 /tmp/mycelium-workflow-launcher.b64 /tmp/mycelium-workflow-launcher.c"
)
$Injection = ($InjectionLines -join "`n")

[IO.File]::WriteAllText(
    $OutputDockerfile,
    $Dockerfile.Replace($Marker, $Injection),
    [Text.UTF8Encoding]::new($false)
)
