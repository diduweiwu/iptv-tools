#!/bin/bash
# merge_m3u.sh —— 合并 m3u 目录下所有文件的组播频道到基准文件
#
# 用途:
#   调用同目录下的 merge_m3u.py, 把 <m3u目录> 中除基准文件外的所有文件里
#   rtp:// 开头的组播频道合并进基准文件(按地址去重、按频道名排序), 写回前
#   自动备份到 <m3u目录>/bak/ 子目录。
#   兼容两种格式: 标准 #EXTINF/group-title 格式与旧版 "名字,rtp://"+"#genre#"
#   格式(可混合), 输出格式跟随基准文件。
#   自动读取脚本同级 config.yaml, 把 params 键下的键值(如 fcc)以查询串
#   形式附加到每个 rtp:// 地址末尾。
#
# 用法:
#   ./merge_m3u.sh                        # 合并脚本同级的 ./m3u 目录, 基准 main.m3u
#   ./merge_m3u.sh /path/to/m3u           # 指定 m3u 目录
#   ./merge_m3u.sh /path/to/m3u other.m3u # 指定 m3u 目录和基准文件名
#
# 退出码: 0 成功; 非 0 失败(透传 python 脚本的返回码)。

set -euo pipefail

# 脚本所在目录(保证从任意位置执行都能找到 merge_m3u.py)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 第 1 个参数: m3u 目录, 默认为脚本同级的 m3u 目录
M3U_DIR="${1:-$SCRIPT_DIR/m3u}"

# 第 2 个参数: 基准文件名(位于 m3u 目录下), 默认 main.m3u
BASE_NAME="${2:-main.m3u}"

# 交给 python 脚本执行, 退出码原样透传
exec python3 "$SCRIPT_DIR/merge_m3u.py" --dir "$M3U_DIR" --base "$BASE_NAME" \
    --config "$SCRIPT_DIR/config.yaml"
