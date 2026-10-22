import uvicorn

from services.common.config import settings

if __name__ == "__main__":
    uvicorn.run("services.api.server:app", host=settings.api_host, port=settings.api_port, reload=True)
