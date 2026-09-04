"""统一数据存储层。

业务模块只与这里暴露的 API 交互，不感知底层实现：

- 集合（整存整取的 JSON 文档）::

    from ..storage import read_collection, write_collection, on_structured_switch

- 关系库连接（逻辑库名 main/feedback/audit/reviewdata）::

    from ..storage import connect_relational
    conn = connect_relational("feedback", FALLBACK_DB_PATH)

- 上传文件目录::

    from ..storage import upload_dir

切换存储后端只需修改 ``data/storage_config.json``（热加载自动生效），
详见 ``storage_config.example.json``。
"""
from .registry import (  # noqa: F401
    connect_relational,
    on_structured_switch,
    read_collection,
    snapshot,
    upload_dir,
    write_collection,
)
from .settings import StorageConfigError, settings  # noqa: F401
