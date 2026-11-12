import argparse
import json
from pathlib import Path
from connector import Connector
from reader import inspect
from lab import serving
from source import Source


def run(fixture):
    with serving(Source(fixture)) as base:
        return inspect(base, fixture)


def collect(fixture, state_path):
    with serving(Source(fixture)) as base:
        return Connector(base, state_path).sync()


def main():
    parser = argparse.ArgumentParser(description="本地来源查看与持久化门店采集")
    parser.add_argument("--fixture", type=Path, default=Path(__file__).parent / "fixtures/source.json")
    parser.add_argument(
        "--state",
        type=Path,
        help="同步状态文件；指定后执行一轮采集并显示视图、待确认窗口与冲突",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="只读取 --state 中的状态并显示，不访问来源",
    )
    arguments = parser.parse_args()
    fixture = json.loads(arguments.fixture.read_text(encoding="utf-8"))

    if arguments.state is None:
        if arguments.status_only:
            parser.error("--status-only 需要 --state")
        print(json.dumps(run(fixture), ensure_ascii=False, indent=2))
        return

    if arguments.status_only:
        # A placeholder origin is enough: status reads persisted data only.
        result = Connector("http://127.0.0.1:1", arguments.state).status()
    else:
        result = collect(fixture, arguments.state)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
