"""打包版 exe 的入口：双击不带参数 = start（一键启动），带参数和 python -m dota_assistant 一样。"""
import sys

from dota_assistant.cli import main


def run() -> int:
    argv = sys.argv[1:] or ["start"]
    try:
        code = main(argv)
    except SystemExit as e:                       # argparse 报参数错误
        code = e.code if isinstance(e.code, int) else 1
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    if code and not sys.argv[1:]:                 # 双击打开的，出错时别让窗口一闪就没了
        try:
            input("\n出错了，看上面的提示（日志在 logs 文件夹）。按回车关闭…")
        except EOFError:
            pass
    return code or 0


if __name__ == "__main__":
    sys.exit(run())
