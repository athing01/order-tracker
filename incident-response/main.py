import os
import json
import subprocess
import datetime
from fastapi import FastAPI, Request, HTTPException
import uvicorn

app = FastAPI()

INCIDENTS_DIR = "incident-response/incidents"
PI_BIN = "/home/athing/.local/bin/pi"
PROJECT_ROOT = "/home/athing/project/learning/order-tracker"

def invoke_pi(prompt: str, timeout: int = 300):
    """Helper to invoke the Pi CLI headlessly."""
    try:
        pi_process = subprocess.run(
            [PI_BIN, "--provider", "litellm", "--model", "gemma4:31b", "--print", "--no-session", prompt],
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

    # --- Phase 1: Coding Assistant ---
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

    coding_output, coding_exit_code = invoke_pi(coding_prompt)

    analysis_file = os.path.join(incident_path, "coding_analysis.txt")
    with open(analysis_file, "w") as f:
        f.write(coding_output)

    # --- Phase 2: Responder ---
    responder_prompt = (
        f"You are the responder phase of Q5. The coding assistant has already completed. "
        f"Incident Directory: {incident_path}. "
        f"Your task: Read the same incident directory, specifically the alert.json, app_logs.txt, "
        f"and the coding assistant's analysis in {analysis_file}. "
        f"Produce the final responder response. "
        f"CRITICAL CONSTRAINTS: Do not implement Q6. Do not modify express-1002. Do not configure Grafana webhooks. "
        f"Do not perform destructive Docker operations. Do not fabricate an incident. "
        f"If this is the 'ResponderTest', acknowledge the test and confirm that the responder is functioning correctly."
    )

    # Inform responder if coding phase failed
    if coding_exit_code != 0:
        responder_prompt += f"\nNOTE: The coding assistant phase failed or timed out (exit code: {coding_exit_code})."

    responder_output, responder_exit_code = invoke_pi(responder_prompt)

    response_file = os.path.join(incident_path, "responder_response.txt")
    with open(response_file, "w") as f:
        f.write(responder_output)

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
