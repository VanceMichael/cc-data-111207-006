# 合作方增量接口的令牌过期与重建

本工具用于查看授权来源的数据及其原始边界，样例中的机构、设备、人员和标识均为虚构。现有入口提供来源预览，不改变来源数据。`connector.py` 提供持久化的门店采集连接器，把全量建立、后续增量与令牌失效后的重建串成连续流程。

## 目录

`source.py` 提供可重复的本地来源；`reader.py` 负责现有读取；`connector.py` 负责持久化同步；`app.py` 是预览与同步入口；`fixtures/source.json` 可替换为同结构样例；`docs/数据约定.md` 描述接口与数据含义。

## 同步入口

每次调用推进一个窗口并返回当前状态：

```bash
python3 app.py --state state/sync.json
python3 app.py --state state/sync.json --status-only
```

输出包含正式视图 `视图`、已确认游标 `游标`、尚未确认完的 `待确认窗口` 与 `冲突`。

- 没有游标时从 `/full` 开始建立全量；只有整条链路走到 `deltaLink` 才发布正式视图。
- 有游标时从该 `deltaLink` 开始下一次增量。空增量原样返回同一 `deltaLink` 属于正常完成。
- 增量返回 410 时丢弃过期游标并从全量重新开始；新全量完成前旧正式视图一直可读。
- 每读完一页就把进度、暂存门店和精确的下一条链接写入状态文件，网络失败或进程重启后从该链接续跑；旧窗口的进度不会与新窗口混用。
- `nextLink` 只允许同源相对或同源绝对链接；跨源链接在发出请求前就被拒绝，窗口内的 `nextLink` 回环会被截住。
- 版本倒退、同版本不同内容记为冲突并且不覆盖当前值；缺省字段保留旧值，显式 `null` 清空字段，`removed=true` 删除门店。

状态文件放在 `state/` 或调用方明确传入的目录，不写入样例文件。来源是本地虚构协议，链接仅供该连接器使用。

## 运行

使用 Python 3.11 及标准库，无第三方依赖。

```bash
python3 app.py
python3 app.py --state /tmp/catalog-state/sync.json
python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/collector-pycache python3 -m compileall -q app.py connector.py lab.py reader.py source.py tests
```

## 容器

基础镜像固定摘要，不在启动阶段安装软件或获取远程数据。

```bash
docker build --pull=false -t delta-catalog:local .
docker run --rm --network none delta-catalog:local
docker run --rm --network none delta-catalog:local python3 -m unittest discover -s tests -v
```

## 本机演示

在全新临时目录执行 `python3 app.py` 可查看实际读取结果，随后运行测试以观察来源边界。这两步使用本地小尺寸材料，通常数秒结束，不需要账户、设备或外部站点。持久化数据应放在 `state/` 或调用方明确传入的目录，不写入样例文件。仓库外不要指向真实渠道账号。
