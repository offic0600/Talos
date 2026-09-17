"""
talos.observability — trace 转发器与 ES 索引模板。

- ``forwarder.py``：从 staging 读取 JSONL，批量发送到 ES，成功后移到 archived。
- ``es-template.json``：ES 索引模板（信封显式映射 + raw 不索引）。
"""
