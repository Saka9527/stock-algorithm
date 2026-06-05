# -*- coding: utf-8 -*-
"""启动多因子回测 API 服务。"""

import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run(
        "api.main:app",
        host=host,
        port=port,
        reload=os.environ.get("API_RELOAD", "").lower() in ("1", "true", "yes"),
    )
