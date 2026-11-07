"""YuanRunner：YuanAgent 本地执行器。

跑在用户 Windows 电脑上的无窗口后台进程：连接中心服务 /ws/runner，
接收文件工具任务（read_file / write_file / list_directory），在用户
指定的工作目录沙箱内执行并回传结果。不依赖 langchain，独立打包。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
