"""Entry point for python -m backend."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=8100,
        log_level="info",
    )
