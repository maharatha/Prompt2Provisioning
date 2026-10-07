from fastapi import FastAPI

app = FastAPI(
    title="Prompt-to-Provisioning Planner",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "prompt-to-provisioning-planner",
    }
