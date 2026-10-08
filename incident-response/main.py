import os
import json
import subprocess
import datetime
from fastapi import FastAPI, Request, HTTPException
import uvicorn

app = FastAPI()

INCIDENTS_DIR = "incident-response/incidents"
PI_BIN = "/home/athing/.local/bin/pi"
CODEX_BIN = "/home/athing/.local/bin/codex"
PROJECT_ROOT = "/home/athing/project/learning/order-tracker"

def invoke_pi(prompt: str, timeout: int = 300):
    """Helper to invoke the Pi CLI headlessly."""
    try:
        pi_process = subprocess.run(
            [
                PI_BIN,
                "--provider", "litellm",
                "--model", "gemma4:31b",
                "--print",
                "--no-session",
                "--tools", "read,bash,edit,write",
                prompt,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        response = pi_process.stdout if pi_process.stdout else pi_process.stderr
        return response, pi_process.returncode
    except subprocess.TimeoutExpired:
        return "Error: Pi process timed out after 300 seconds.", -1
    except Exception as e:
        return f"Error starting Pi process: {str(e)}", -1

def invoke_codex(prompt: str, timeout: int = 300, sandbox: str = "workspace-write"):
    """Invoke Codex CLI headlessly and preserve stdout/stderr separately."""
    try:
        codex_process = subprocess.run(
            [
                CODEX_BIN,
                "-m", "gemma4:31b",
                "-c", 'approval_policy="never"',
                "exec",
                "-s", sandbox,
                prompt,
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (
            codex_process.stdout,
            codex_process.stderr,
            codex_process.returncode,
        )
    except subprocess.TimeoutExpired:
        return (
            "",
            f"Codex process timed out after {timeout} seconds.",
            -1,
        )
    except Exception as e:
        return (
            "",
            f"Error starting Codex process: {str(e)}",
            -1,
        )


def deploy_app(timeout: int = 180):
    """Deploy only the app service after a validated source mutation."""
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "up",
                "-d",
                "--build",
                "--no-deps",
                "app",
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"App deployment timed out after {timeout} seconds.", -1
    except Exception as e:
        return "", f"Error deploying app: {str(e)}", -1


@app.post("/alerts")
async def handle_alert(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # 1. Save alert/evidence
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    incident_id = f"incident_{timestamp}"
    # Use absolute path for incident directory to ensure Pi can find it regardless of CWD
    abs_incidents_dir = os.path.abspath(INCIDENTS_DIR)
    incident_path = os.path.join(abs_incidents_dir, incident_id)
    os.makedirs(incident_path, exist_ok=True)

    with open(os.path.join(incident_path, "alert.json"), "w") as f:
        json.dump(payload, f, indent=2)

    # Simple evidence collection: Capture app container logs
    try:
        log_result = subprocess.run(
            ["docker", "compose", "logs", "--tail", "100", "app"],
            capture_output=True, text=True, timeout=10
        )
        with open(os.path.join(incident_path, "app_logs.txt"), "w") as f:
            f.write(log_result.stdout)
    except Exception as e:
        with open(os.path.join(incident_path, "error_logs.txt"), "w") as f:
            f.write(str(e))

    # Extract alert details for the prompt
    alert_info = payload.get("alerts", [{}])[0]
    alert_name = alert_info.get("labels", {}).get("alertname", "Unknown")
    summary = alert_info.get("annotations", {}).get("summary", "No summary provided")
    is_q6_incident = alert_name == "HTTP 5xx Errors"

    if is_q6_incident:
        def run_capture(args, timeout=30):
            result = subprocess.run(
                args,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return (
                f"$ {' '.join(args)}\\n"
                f"exit_code={result.returncode}\\n"
                f"--- stdout ---\\n{result.stdout}\\n"
                f"--- stderr ---\\n{result.stderr}\\n"
            )

        with open(os.path.join(incident_path, "pre_fix_git_status.txt"), "w") as f:
            f.write(run_capture(["git", "status", "--short"]))

        with open(os.path.join(incident_path, "pre_fix_app_start.txt"), "w") as f:
            app_container = subprocess.run(
                ["docker", "compose", "ps", "-q", "app"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
            if app_container:
                f.write(
                    run_capture(
                        [
                            "docker",
                            "inspect",
                            app_container,
                            "--format",
                            "{{.State.StartedAt}}",
                        ]
                    )
                )
            else:
                f.write("app container not found\\n")

        coding_prompt = (
            f"You are the coding agent for Homework 4 Q6 automated incident remediation. "
            f"Incident Directory: {incident_path}. "
            f"Alert: {alert_name}. Summary: {summary}. "
            f"Repository Root: {PROJECT_ROOT}. "
            f"Read alert.json and app_logs.txt first, then investigate the actual application defect "
            f"causing the HTTP 5xx. "
            f"You MUST fix the real application defect causing the alerted request to fail. "
            f"Expected target request for verification: GET http://127.0.0.1:8000/api/orders/express-1002. "
            f"You may modify application source files under app/ only. "
            f"Do not modify incident-response/, observability/, Grafana configuration, or webhook configuration. "
            f"Do not change unrelated endpoints or manufacture a different fault. "
            f"Do not use destructive Docker operations, especially 'docker compose down -v'. "
            f"Do not run Docker commands and do not attempt to rebuild or restart containers. "
            f"The remediation controller will deploy the validated source change after your edit. "
            f"Do not claim live HTTP success based on mocks or hypothetical results. "
            f"Do not commit or push any changes. "
            f"Report exactly what you investigated and what application code you changed. "
            f"Do not claim that the live application was restarted or verified by you. "
            f"The remediation controller will perform deployment and live verification."
        )

        responder_prompt = (
            f"You are the final responder phase of Homework 4 Q6. "
            f"Incident Directory: {incident_path}. "
            f"Read alert.json, app_logs.txt, coding_analysis.txt, and the current repository state. "
            f"Verify the remediation result rather than fabricating success. "
            f"Check the application container state and verify GET http://127.0.0.1:8000/api/orders/express-1002. "
            f"The expected final status is HTTP 200. "
            f"Also verify that standard-1002 remains HTTP 404. "
            f"Do not modify Grafana, webhook configuration, incident-response, or observability. "
            f"Do not perform destructive Docker operations. "
            f"Do not commit or push. "
            f"Produce a concise final incident response stating whether Q6 remediation succeeded, "
            f"what the coding agent changed, whether the application restarted, and the verification results."
        )
    else:
        coding_prompt = (
            f"You are the coding assistant phase of Q5. This is an automated headless incident-analysis step. "
            f"Incident Directory: {incident_path}. "
            f"Alert: {alert_name}. Summary: {summary}. "
            f"Repository Root: {PROJECT_ROOT}. "
            f"Your task: Read the incident evidence (alert.json, app_logs.txt) from the supplied directory, "
            f"analyze the problem, and produce a concise incident analysis for the responder. "
            f"CRITICAL CONSTRAINTS: Do not modify the repository. Do not implement Q6. Do not touch express-1002. "
            f"Do not perform destructive Docker operations. "
            f"If this is the 'ResponderTest', acknowledge that it is a test notification and no incident exists."
        )

        responder_prompt = (
            f"You are the responder phase of Q5. The coding assistant has already completed. "
            f"Incident Directory: {incident_path}. "
            f"Your task: Read the same incident directory, specifically the alert.json, app_logs.txt, "
            f"and the coding assistant's analysis in {os.path.join(incident_path, 'coding_analysis.txt')}. "
            f"Produce the final responder response. "
            f"CRITICAL CONSTRAINTS: Do not implement Q6. Do not modify express-1002. Do not configure Grafana webhooks. "
            f"Do not perform destructive Docker operations. Do not fabricate an incident. "
            f"If this is the 'ResponderTest', acknowledge the test and confirm that the responder is functioning correctly."
        )

    if is_q6_incident:
        coding_stdout, coding_stderr, coding_exit_code = invoke_codex(
            coding_prompt,
            timeout=300,
            sandbox="workspace-write",
        )
        coding_output = coding_stdout if coding_stdout else coding_stderr
    else:
        coding_output, coding_exit_code = invoke_pi(coding_prompt)

    analysis_file = os.path.join(incident_path, "coding_analysis.txt")
    with open(analysis_file, "w") as f:
        f.write(coding_output)

    if is_q6_incident:
        with open(os.path.join(incident_path, "coding_stdout.txt"), "w") as f:
            f.write(coding_stdout)

        with open(os.path.join(incident_path, "coding_stderr.txt"), "w") as f:
            f.write(coding_stderr)

        with open(os.path.join(incident_path, "coding_exit_code.txt"), "w") as f:
            f.write(f"{coding_exit_code}\n")

        post_coding_diff = subprocess.run(
            ["git", "diff", "HEAD", "--", "app/"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        with open(
            os.path.join(incident_path, "post_coding_git_diff.txt"), "w"
        ) as f:
            f.write(post_coding_diff.stdout)

        post_coding_status = subprocess.run(
            ["git", "status", "--short", "app/"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        with open(
            os.path.join(incident_path, "post_coding_git_status.txt"), "w"
        ) as f:
            f.write(post_coding_status.stdout)

        app_changed = bool(post_coding_diff.stdout.strip())
        with open(
            os.path.join(incident_path, "post_coding_mutation_gate.txt"), "w"
        ) as f:
            f.write(f"app_changed={app_changed}\n")

        if not app_changed:
            responder_prompt += (
                "\nMANDATORY MUTATION GATE: The coding agent produced no git diff "
                "under app/. Treat the remediation as failed unless the current "
                "application source genuinely changed before your verification.\n"
            )

        if app_changed:
            deployment_stdout, deployment_stderr, deployment_exit_code = deploy_app()

            with open(
                os.path.join(incident_path, "deployment_stdout.txt"), "w"
            ) as f:
                f.write(deployment_stdout)

            with open(
                os.path.join(incident_path, "deployment_stderr.txt"), "w"
            ) as f:
                f.write(deployment_stderr)

            with open(
                os.path.join(incident_path, "deployment_exit_code.txt"), "w"
            ) as f:
                f.write(f"{deployment_exit_code}\n")

            if deployment_exit_code != 0:
                responder_prompt += (
                    "\nMANDATORY DEPLOYMENT GATE: The controller attempted "
                    "docker compose up -d --build --no-deps app but deployment "
                    f"failed with exit code {deployment_exit_code}. "
                    "Treat the remediation as failed.\n"
                )

    if coding_exit_code != 0:
        responder_prompt += (
            f"\\nNOTE: The coding assistant phase failed or timed out "
            f"(exit code: {coding_exit_code})."
        )

    # Inform responder if coding phase failed
    if coding_exit_code != 0:
        responder_prompt += f"\nNOTE: The coding assistant phase failed or timed out (exit code: {coding_exit_code})."

    responder_output, responder_exit_code = invoke_pi(responder_prompt)

    response_file = os.path.join(incident_path, "responder_response.txt")
    with open(response_file, "w") as f:
        f.write(responder_output)

    if is_q6_incident:
        def capture_post_fix(args, timeout=30):
            result = subprocess.run(
                args,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return (
                f"$ {' '.join(args)}\\n"
                f"exit_code={result.returncode}\\n"
                f"--- stdout ---\\n{result.stdout}\\n"
                f"--- stderr ---\\n{result.stderr}\\n"
            )

        with open(os.path.join(incident_path, "post_fix_git_diff.txt"), "w") as f:
            f.write(capture_post_fix(["git", "diff", "--", "app/"]))

        with open(os.path.join(incident_path, "post_fix_git_status.txt"), "w") as f:
            f.write(capture_post_fix(["git", "status", "--short"]))

        with open(os.path.join(incident_path, "post_fix_compose.txt"), "w") as f:
            f.write(capture_post_fix(["docker", "compose", "ps"]))

        with open(os.path.join(incident_path, "post_fix_app_start.txt"), "w") as f:
            app_container = subprocess.run(
                ["docker", "compose", "ps", "-q", "app"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
            if app_container:
                f.write(
                    capture_post_fix(
                        [
                            "docker",
                            "inspect",
                            app_container,
                            "--format",
                            "{{.State.StartedAt}}",
                        ]
                    )
                )
            else:
                f.write("app container not found\\n")

        verification = subprocess.run(
            [
                "curl",
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "express-1002 HTTP %{http_code}\\n",
                "http://127.0.0.1:8000/api/orders/express-1002",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        standard_verification = subprocess.run(
            [
                "curl",
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "standard-1002 HTTP %{http_code}\\n",
                "http://127.0.0.1:8000/api/orders/standard-1002",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

        express_ok = (
            verification.returncode == 0
            and verification.stdout.strip() == "express-1002 HTTP 200"
        )
        standard_ok = (
            standard_verification.returncode == 0
            and standard_verification.stdout.strip() == "standard-1002 HTTP 404"
        )

        with open(os.path.join(incident_path, "post_fix_verification.txt"), "w") as f:
            f.write(verification.stdout)
            f.write(standard_verification.stdout)
            f.write(f"express_ok={express_ok}\n")
            f.write(f"standard_ok={standard_ok}\n")

        if not express_ok or not standard_ok:
            responder_exit_code = 1

    if responder_exit_code != 0:
        raise HTTPException(
            status_code=502,
            detail={
                "incident_id": incident_id,
                "coding_exit_code": coding_exit_code,
                "responder_exit_code": responder_exit_code,
                "message": (
                    "Responder failed or timed out. "
                    "See responder_response.txt for captured output."
                ),
            },
        )

    # Return consolidated result
    return {
        "incident_id": incident_id,
        "coding_analysis": coding_output,
        "coding_exit_code": coding_exit_code,
        "responder_response": responder_output,
        "responder_exit_code": responder_exit_code
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
