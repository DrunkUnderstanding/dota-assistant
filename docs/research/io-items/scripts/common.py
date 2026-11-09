"""三个脚本共用：研究哪个英雄（--target 英雄 id，默认艾欧 91）、数据放哪。
艾欧的数据沿用最早的目录 data/research/io-items/，别的英雄放 data/research/hero-items-<id>/。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core.config import DATA_DIR  # noqa: E402


def take_target(argv: list[str]) -> int:
    """从命令行里取出 --target N（并删掉，剩下的给各脚本自己解析）"""
    if "--target" in argv:
        i = argv.index("--target")
        hero = int(argv[i + 1])
        del argv[i:i + 2]
        return hero
    return 91


def data_dir(hero: int) -> Path:
    d = DATA_DIR / "research" / ("io-items" if hero == 91 else f"hero-items-{hero}")
    d.mkdir(parents=True, exist_ok=True)
    return d
