from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
import subprocess
import tempfile
import os
import shutil
import time
import json
import uuid

app = FastAPI(title="Luscent Remote Sandbox")

# Simple API Key security (set this in Cloud Run env)
API_KEY = os.environ.get("QAI_RUNNER_SECRET")
api_key_header = APIKeyHeader(name="X-QAi-Secret")

async def get_api_key(api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return api_key

class TestRequest(BaseModel):
    script: str
    env_vars: dict = {}
    timeout: int = 60

@app.post("/execute")
async def execute_test(req: TestRequest, api_key: str = Depends(get_api_key)):
    # 1. Prepare Workspace
    run_id = str(uuid.uuid4())
    temp_dir = tempfile.mkdtemp(prefix=f"run_{run_id}_")
    script_path = os.path.join(temp_dir, "test_script.py")
    
    with open(script_path, "w") as f:
        f.write(req.script)

    try:
        # 2. Execution Environment
        env = os.environ.copy()
        env.update(req.env_vars)
        env["SCREENSHOT_PATH"] = os.path.join(temp_dir, "screenshot.png")

        start_time = time.time()
        
        # We run as the 'qairunner' user if possible, but Cloud Run handles most of this
        process = subprocess.run(
            ["python", "test_script.py"],
            cwd=temp_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=req.timeout
        )
        
        duration = int((time.time() - start_time) * 1000)

        # 3. Handle Artifacts (Screenshots)
        has_screenshot = os.path.exists(env["SCREENSHOT_PATH"])
        screenshot_b64 = None
        
        if has_screenshot:
            import base64
            with open(env["SCREENSHOT_PATH"], "rb") as img_f:
                screenshot_b64 = base64.b64encode(img_f.read()).decode('utf-8')
        
        return {
            "stdout": process.stdout,
            "stderr": process.stderr,
            "returncode": process.returncode,
            "duration": duration,
            "has_screenshot": has_screenshot,
            "screenshot_b64": screenshot_b64
        }

    except subprocess.TimeoutExpired:
        return {"error": "Timeout", "returncode": -1, "duration": req.timeout * 1000}
    except Exception as e:
        return {"error": str(e), "returncode": -1, "duration": 0}
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

@app.get("/health")
async def health():
    return {"status": "ready", "isolation": "gvisor" if os.path.exists("/proc/sys/kernel/ostype") else "container"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
