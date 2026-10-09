"""麦麦精算师异常定义。"""


class McdError(Exception):
    """所有麦当劳 MCP 相关异常的基类。"""


class McpAuthError(McdError):
    """Token 无效 / 缺失（HTTP 401）。"""


class McpRateLimitError(McdError):
    """触发官方限流（HTTP 429，600 次/分钟）。"""


class McpTransportError(McdError):
    """网络或传输层错误。"""


class McpToolError(McdError):
    """MCP 工具返回业务错误。"""


class PlannerError(McdError):
    """规划器输入/求解错误。"""
