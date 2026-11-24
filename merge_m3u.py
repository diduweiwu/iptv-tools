#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_m3u.py —— 组播频道合并工具

用途:
    扫描 m3u 目录下的所有文件, 把其中 rtp:// 开头的组播频道合并进基准文件
    (默认 main.m3u):
      1. 保留 "#genre#" 频道分组, 未声明分组的频道归入默认分组(默认 "其它");
      2. 按组播地址去重: 同一地址只保留首次出现的条目, 同名频道的新地址
         会追加到已有频道后面(一个频道允许多个地址);
      3. 每个分组内的频道按名称排序(有 pypinyin 时按拼音, 数字按数值);
      4. 写回前自动把基准文件备份到 m3u 目录下的 bak/ 子目录
         (如 m3u/bak/main.m3u.bak)。

文件格式约定:
    分组行:  央视,#genre#
    频道行:  CCTV1,rtp://239.11.0.65:5140

使用示例:
    python3 merge_m3u.py --dir m3u --base main.m3u
    python3 merge_m3u.py --dir /path/to/m3u --base main.m3u --no-backup
    python3 merge_m3u.py --dir m3u --sort natural   # 不用拼音, 按字符排序
    python3 merge_m3u.py --dir m3u --sort none      # 保持原出现顺序不排序

退出码: 0 成功; 1 参数/运行出错。
"""

import argparse
import re
import shutil
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

try:
    from pypinyin import lazy_pinyin
    HAS_PINYIN = True
except ImportError:
    HAS_PINYIN = False

GENRE_SUFFIX = '#genre#'    # 频道分组行后缀, 如: 央视,#genre#
RTP_PREFIX = 'rtp://'       # 组播地址前缀, 只合并这类地址
EXTINF_PREFIX = '#EXTINF'   # 标准 m3u 扩展信息行前缀(用于兼容其频道名)
DEFAULT_GROUP = '其它'       # 未声明分组的频道默认归入的分组名
DETAIL_LIMIT = 10           # 摘要中最多展示的"异名重复地址"明细条数
UNGROUPED = ''              # 解析时"未声明分组"的占位分组名


@dataclass
class Channel:
    """频道条目: 频道名 + 有序去重的组播地址列表。

    属性:
        name: 频道名称, 如 "CCTV1"。
        urls: 该频道的组播地址列表, 按出现顺序保存且不重复。

    使用示例:
        ch = Channel('CCTV1')
        ch.add_url('rtp://239.11.0.65:5140')  # 返回 True, 追加成功
        ch.add_url('rtp://239.11.0.65:5140')  # 返回 False, 地址已存在被忽略
    """

    name: str
    urls: List[str] = field(default_factory=list)

    def add_url(self, url: str) -> bool:
        """追加一个组播地址(地址已存在则忽略)。

        参数:
            url: 组播地址, 如 "rtp://239.11.0.65:5140"。
        返回:
            True 表示新追加成功; False 表示地址已存在被忽略。
        """
        if url in self.urls:
            return False
        self.urls.append(url)
        return True


@dataclass
class MergeStat:
    """单个源文件的合并统计。

    属性:
        source: 源文件名(仅文件名, 不含路径)。
        added: 新合并进基准的地址条数。
        skipped: 因地址已存在而被跳过的条数。
        duplicates: 异名重复明细列表, 每项为 (地址, 源频道名, 已有频道名)。
    """

    source: str
    added: int = 0
    skipped: int = 0
    duplicates: List[Tuple[str, str, str]] = field(default_factory=list)


class ChannelSorter:
    """频道排序策略基类(策略模式), 子类实现 sort_key 即可定制排序规则。"""

    def sort_key(self, name: str) -> Tuple:
        """计算频道名的排序键。

        参数:
            name: 频道名, 如 "CCTV10"。
        返回:
            可逐元素比较的排序键元组。
        """
        raise NotImplementedError

    def sort(self, channels: List[Channel]) -> List[Channel]:
        """对频道列表做稳定排序(同名的保持原有先后顺序)。

        参数:
            channels: 频道列表。
        返回:
            排序后的新列表, 不改动传入列表。
        """
        return sorted(channels, key=lambda ch: self.sort_key(ch.name))


class IdentitySorter(ChannelSorter):
    """不排序策略: 保持频道原有出现顺序。"""

    def sort_key(self, name: str) -> Tuple:
        """排序键恒为空元组, 等价于不排序。

        参数:
            name: 频道名。
        返回:
            空元组。
        """
        return ()

    def sort(self, channels: List[Channel]) -> List[Channel]:
        """原样返回频道列表副本。

        参数:
            channels: 频道列表。
        返回:
            频道列表的浅拷贝。
        """
        return list(channels)


class NaturalSorter(ChannelSorter):
    """自然排序(降级方案): 数字段按数值比较(如 CCTV2 < CCTV10), 文字段按小写字符比较。"""

    def sort_key(self, name: str) -> Tuple:
        """计算频道名的排序键。

        频道名按连续数字切分: 数字段 -> (0, 数值), 文字段 -> (1, 文本键),
        数字段排在文字段之前(如 "4K电影" 排在最前)。

        参数:
            name: 频道名, 如 "CCTV10"。
        返回:
            排序键元组, 如 ((1, 'cctv'), (0, 10))。
        """
        return tuple(
            (0, int(part)) if part.isdigit() else (1, self.text_key(part))
            for part in re.split(r'(\d+)', name) if part
        )

    @staticmethod
    def text_key(text: str) -> str:
        """计算文字段的排序键。

        参数:
            text: 非纯数字的文字片段。
        返回:
            小写字符串。
        """
        return text.lower()


class PinyinSorter(NaturalSorter):
    """拼音自然排序: 中文按拼音、数字按数值, 符合常见频道表习惯。

    依赖 pypinyin; 未安装时由 create_sorter 自动降级为 NaturalSorter。

    使用示例:
        PinyinSorter().text_key('四川卫视')  # 'sichuanweishi'
    """

    @staticmethod
    def text_key(text: str) -> str:
        """计算文字段的排序键: 转为小写拼音串。

        参数:
            text: 含中文的文字片段, 如 "卫视"。
        返回:
            小写拼音串, 如 "weishi"。
        """
        return ''.join(lazy_pinyin(text)).lower()


def create_sorter(mode: str = 'auto') -> ChannelSorter:
    """排序策略工厂(工厂模式): 根据模式名创建对应排序器。

    参数:
        mode: 排序模式, 取值:
            'auto'    - 已安装 pypinyin 则拼音排序, 否则自然排序;
            'pinyin'  - 强制拼音排序, 未安装 pypinyin 时报错;
            'natural' - 自然排序(数字按数值, 其余按字符);
            'none'    - 不排序, 保持原有出现顺序。
    返回:
        ChannelSorter 排序器实例。
    异常:
        ValueError: 指定 'pinyin' 但未安装 pypinyin 时抛出。
    """
    if mode == 'none':
        return IdentitySorter()
    if HAS_PINYIN and mode in ('auto', 'pinyin'):
        return PinyinSorter()
    if mode == 'pinyin':
        raise ValueError(
            '排序方式为 pinyin 但未安装 pypinyin, '
            '请先执行: pip3 install --user pypinyin, 或改用 --sort natural'
        )
    return NaturalSorter()


class M3uParser:
    """m3u 文本解析器(适配器模式): 把文件解析为 分组 -> 频道 的有序结构。"""

    @staticmethod
    def read_text(path: Path) -> str:
        """读取文件全部文本。

        优先按 UTF-8(容忍 BOM)读取; 遇到非 UTF-8 内容时降级按 GBK 读取。

        参数:
            path: m3u 文件路径。
        返回:
            文件文本内容。
        异常:
            OSError: 文件不存在或不可读时抛出。
        """
        try:
            return path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            return path.read_text(encoding='gbk', errors='replace')

    @classmethod
    def parse(cls, path: Path) -> 'OrderedDict[str, OrderedDict[str, Channel]]':
        """解析单个 m3u 文件。

        识别三类行:
            分组行 "xxx,#genre#"  -> 切换当前分组(不存在的分组会创建);
            扩展行 "#EXTINF:..."  -> 暂存频道名, 供下一行裸地址使用;
            频道行 "名字,rtp://.." -> 登记频道与地址。
        其余行(空行、#EXTM3U 等)忽略。

        参数:
            path: m3u 文件路径。
        返回:
            OrderedDict{分组名: OrderedDict{频道名: Channel}},
            未声明分组的频道放在分组名 UNGROUPED('')下, 各层均保持出现顺序。
        异常:
            OSError: 文件不可读时抛出。
        """
        groups: 'OrderedDict[str, OrderedDict[str, Channel]]' = OrderedDict()
        current = UNGROUPED           # 当前分组名, 文件开头未声明分组时为 ''
        pending_name: Optional[str] = None  # EXTINF 行暂存的频道名
        for raw in cls.read_text(path).splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.endswith(GENRE_SUFFIX):
                current = line[:-len(GENRE_SUFFIX)].strip().strip(',')
                groups.setdefault(current, OrderedDict())
                pending_name = None
            elif line.upper().startswith(EXTINF_PREFIX):
                pending_name = line.rsplit(',', 1)[-1].strip()
            elif RTP_PREFIX in line:
                cls._register_line(groups, current, line, pending_name)
        return groups

    @staticmethod
    def _register_line(groups: 'OrderedDict[str, OrderedDict[str, Channel]]',
                       current: str, line: str,
                       pending_name: Optional[str]) -> None:
        """登记一行频道记录, 形如 "CCTV1,rtp://239.11.0.65:5140"。

        带逗号的行按 "频道名,地址" 拆分; 不带逗号的裸地址行使用 EXTINF
        暂存的频道名; 无法确定频道名的行直接忽略。

        参数:
            groups: 解析结果容器(原地更新)。
            current: 当前分组名。
            line: 已去除首尾空白的原始行。
            pending_name: EXTINF 行暂存的频道名, 可为 None。
        """
        if ',' in line:
            name, url = line.split(',', 1)
            name, url = name.strip(), url.strip()
        else:
            name, url = (pending_name or '').strip(), line.strip()
        if not name:
            return
        bucket = groups.setdefault(current, OrderedDict())
        channel = bucket.setdefault(name, Channel(name))
        channel.add_url(url)


class M3uMerger:
    """把源文件的频道合并进基准分组结构(按组播地址去重)。

    去重规则: 同一地址只保留首次出现的条目; 同名频道的新地址追加到
    已有频道后面(保持"一个频道多个地址"的形态)。

    使用示例:
        base = M3uParser.parse(Path('m3u/main.m3u'))
        merger = M3uMerger(base, default_group='其它')
        stat = merger.merge_file(Path('m3u/source_1.m3u'))
    """

    def __init__(self, base_groups: 'OrderedDict[str, OrderedDict[str, Channel]]',
                 default_group: str = DEFAULT_GROUP):
        """初始化合并器并登记基准文件中已有的全部地址。

        参数:
            base_groups: 基准文件的分组结构(原地更新, 即合并结果)。
            default_group: 未声明分组的频道落点分组名。
        """
        self.groups = base_groups
        self.default_group = default_group
        self.seen = {}  # 地址 -> (分组名, 频道名), 记录首次登记位置
        for gname, channels in self.groups.items():
            for channel in channels.values():
                for url in channel.urls:
                    self.seen.setdefault(url, (gname, channel.name))

    def merge_file(self, path: Path) -> MergeStat:
        """合并一个源文件。

        参数:
            path: 源 m3u 文件路径。
        返回:
            该文件的合并统计 MergeStat。
        异常:
            OSError: 源文件不可读时抛出。
        """
        source_groups = M3uParser.parse(path)
        stat = MergeStat(source=path.name)
        for gname, channels in source_groups.items():
            target_group = self._resolve_group(gname)
            for channel in channels.values():
                self._merge_channel(target_group, channel, stat)
        return stat

    def _resolve_group(self, gname: str) -> str:
        """确定源分组对应的落点分组名。

        规则: 源文件声明了分组则保留该分组名(基准中不存在时会在写入
        频道时追加为末尾新分组, 实现"分组保留"); 未声明分组则归入默认分组。

        参数:
            gname: 源文件中的分组名, 未声明分组时为 UNGROUPED('')。
        返回:
            落点分组名。
        """
        return gname if gname else self.default_group

    def _merge_channel(self, target_group: str, channel: Channel,
                       stat: MergeStat) -> None:
        """合并单个频道的所有地址。

        地址已登记过则跳过并计数(频道名不同时记录明细); 新地址交给
        _append_url 处理。

        参数:
            target_group: 该频道的落点分组名。
            channel: 源频道条目。
            stat: 本源文件的统计对象(原地累计)。
        """
        for url in channel.urls:
            hit = self.seen.get(url)
            if hit:
                stat.skipped += 1
                if hit[1] != channel.name:
                    stat.duplicates.append((url, channel.name, hit[1]))
                continue
            self._append_url(target_group, channel.name, url, stat)

    def _append_url(self, target_group: str, name: str, url: str,
                    stat: MergeStat) -> None:
        """把一个新地址并入基准结构。

        规则: 基准中若已有同名频道(任意分组)则把地址追加给它;
        否则在落点分组中新建频道(分组不存在时顺带创建, 追加在末尾)。

        参数:
            target_group: 落点分组名。
            name: 频道名。
            url: 新组播地址。
            stat: 本源文件的统计对象(原地累计)。
        """
        for channels in self.groups.values():
            if name in channels:
                channels[name].add_url(url)
                break
        else:
            bucket = self.groups.setdefault(target_group, OrderedDict())
            bucket[name] = Channel(name)
            bucket[name].add_url(url)
        self.seen[url] = (target_group, name)
        stat.added += 1


class M3uRenderer:
    """把合并后的分组结构渲染回 m3u 文本(模板方法模式的变体:
    render 固定输出骨架, 频道顺序由注入的排序策略决定)。"""

    def __init__(self, sorter: ChannelSorter):
        """初始化渲染器。

        参数:
            sorter: 频道排序策略。
        """
        self.sorter = sorter

    def render(self, groups: 'OrderedDict[str, OrderedDict[str, Channel]]') -> str:
        """渲染为 m3u 文本。

        每个分组输出一行 "分组名,#genre#" 及其频道行, 分组之间用空行
        分隔; 空分组不输出; 末尾带换行符。

        参数:
            groups: 合并完成后的分组结构。
        返回:
            完整的 m3u 文本。
        """
        blocks = []
        for gname, channels in groups.items():
            if not channels:
                continue
            lines = ['{},{}'.format(gname, GENRE_SUFFIX)]
            for channel in self.sorter.sort(list(channels.values())):
                lines.extend('{},{}'.format(channel.name, url)
                             for url in channel.urls)
            blocks.append('\n'.join(lines))
        return '\n\n'.join(blocks) + '\n'


def get_backup_path(m3u_dir: Path, base_path: Path) -> Path:
    """计算基准文件的备份路径: 统一放在 m3u 目录下的 bak 子目录中。

    参数:
        m3u_dir: m3u 目录路径。
        base_path: 基准文件路径。
    返回:
        备份文件路径, 如 "m3u/bak/main.m3u.bak"。

    使用示例:
        get_backup_path(Path('m3u'), Path('m3u/main.m3u'))  # m3u/bak/main.m3u.bak
    """
    return m3u_dir / 'bak' / (base_path.name + '.bak')


def iter_source_files(m3u_dir: Path, base_path: Path) -> Iterator[Path]:
    """按文件名顺序迭代目录下需要合并的源文件。

    只扫描目录第一层的普通文件; 排除: 基准文件本身、子目录
    (如 bak/ 备份目录)、隐藏文件(如 .DS_Store)、*.bak 备份文件。

    参数:
        m3u_dir: m3u 目录路径。
        base_path: 基准文件路径。
    返回:
        源文件路径迭代器(按文件名排序, 保证多次运行结果稳定)。
    """
    for path in sorted(m3u_dir.iterdir()):
        if (path.is_file() and path != base_path
                and not path.name.startswith('.')
                and not path.name.endswith('.bak')):
            yield path


def print_summary(stats: List[MergeStat],
                  groups: 'OrderedDict[str, OrderedDict[str, Channel]]',
                  base_path: Path, backup_path: Path) -> None:
    """打印合并统计与结果概览。

    参数:
        stats: 各源文件的合并统计列表。
        groups: 合并完成后的分组结构。
        base_path: 被写回的基准文件路径。
        backup_path: 备份文件路径(未备份时为 None)。
    """
    print('===== 合并统计 =====')
    for stat in stats:
        print('{}: 新增 {} 条, 跳过重复地址 {} 条'.format(
            stat.source, stat.added, stat.skipped))
    duplicates = [item for stat in stats for item in stat.duplicates]
    if duplicates:
        print('-- 因地址已存在而跳过、且源频道名与已有频道名不同的明细 '
              '(共 {} 条, 最多展示 {} 条) --'.format(len(duplicates), DETAIL_LIMIT))
        for url, src_name, exist_name in duplicates[:DETAIL_LIMIT]:
            print('  {}  源[{}] -> 已有[{}]'.format(url, src_name, exist_name))
        if len(duplicates) > DETAIL_LIMIT:
            print('  …… 等共 {} 条'.format(len(duplicates)))
    channel_count = sum(len(channels) for channels in groups.values())
    url_count = sum(len(channel.urls)
                    for channels in groups.values()
                    for channel in channels.values())
    print('===== 结果 =====')
    print('分组 {} 个, 频道 {} 个, 组播地址 {} 条'.format(
        len(groups), channel_count, url_count))
    print('已写回: {}'.format(base_path))
    if backup_path is not None:
        print('已备份原文件: {}'.format(backup_path))


def run(args: argparse.Namespace) -> int:
    """执行合并主流程: 解析基准文件 -> 合并各源文件 -> 写回并打印摘要。

    参数:
        args: 命令行参数(--dir/--base/--default-group/--sort/--no-backup)。
    返回:
        0 成功; 1 出错(如基准文件不存在、排序参数非法)。
    """
    m3u_dir = Path(args.dir).expanduser()
    base_path = m3u_dir / args.base
    if not m3u_dir.is_dir():
        print('错误: m3u 目录不存在: {}'.format(m3u_dir), file=sys.stderr)
        return 1
    if not base_path.is_file():
        print('错误: 基准文件不存在: {}'.format(base_path), file=sys.stderr)
        return 1
    try:
        sorter = create_sorter(args.sort)
    except ValueError as exc:
        print('错误: {}'.format(exc), file=sys.stderr)
        return 1

    base_groups = M3uParser.parse(base_path)
    merger = M3uMerger(base_groups, default_group=args.default_group)
    stats = [merger.merge_file(path) for path in iter_source_files(m3u_dir, base_path)]

    backup_path = None
    if not args.no_backup:
        backup_path = get_backup_path(m3u_dir, base_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base_path, backup_path)
    base_path.write_text(M3uRenderer(sorter).render(merger.groups), encoding='utf-8')
    print_summary(stats, merger.groups, base_path, backup_path)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    返回:
        配置好各参数与中文帮助信息的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(
        description='合并 m3u 目录下所有文件的组播频道(rtp://)到基准文件')
    parser.add_argument('--dir', default='m3u',
                        help='m3u 文件目录, 默认: ./m3u')
    parser.add_argument('--base', default='main.m3u',
                        help='基准文件名(位于 --dir 下), 其他文件合并进它, 默认: main.m3u')
    parser.add_argument('--default-group', default=DEFAULT_GROUP,
                        help='未声明分组频道的落点分组名, 默认: {}'.format(DEFAULT_GROUP))
    parser.add_argument('--sort', default='auto',
                        choices=['auto', 'pinyin', 'natural', 'none'],
                        help='频道排序方式: auto=有 pypinyin 用拼音否则自然排序(默认); '
                             'pinyin=拼音; natural=自然排序; none=不排序')
    parser.add_argument('--no-backup', action='store_true',
                        help='覆盖基准文件前不备份到 bak 目录')
    return parser


def main() -> None:
    """命令行入口: 解析参数并执行合并, 出错时输出中文错误信息。

    使用示例:
        python3 merge_m3u.py --dir m3u --base main.m3u
    """
    args = build_arg_parser().parse_args()
    try:
        sys.exit(run(args))
    except OSError as exc:
        print('错误: 文件读写失败: {}'.format(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
