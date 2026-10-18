#!/usr/bin/env python3
"""
KSP CFG 可本地化字段提取与 MM 补丁生成器

用法：
    python auto_extractor.py <BeyondHome目录> [模组名] [--out 输出目录] [--debug]

生成：
    <模组名>_Localization_Patch.cfg       把可本地化字段替换为 #autoLOC_ 键
    <模组名>_Localization_Dictionary.cfg  英文原文字典（供翻译）

相对旧版的修复：
1. 节点头解析支持 :HAS[...] 等过滤器，不再把选择器混进节点名；
2. 顶层补丁统一加 :FINAL，保证在 BeyondHome 把 Body 挂到 Kopernicus 之后才执行；
3. Body 不能嵌套 Body，嵌套结构会被提升到 Kopernicus 下一层；
4. 没有选择器的 Body 补丁直接跳过，避免把某个天体的文案套到所有天体上；
5. 注释只在不位于引号内、也不在 URL 中时剥离；
6. 值两端的引号会被正确去掉，字典输出时再统一转义；
7. 输出目录可指定，默认优先写到 GameData/SaveRhode/<模组名>。
"""

import os
import sys
import re
import hashlib
from collections import OrderedDict

# ----------------------------------------------------------------------
# 可本地化字段映射表
# ----------------------------------------------------------------------
LOCALIZABLE_FIELDS = {
    "AGENT": ["title", "description"],
    "B9_TANK_TYPE": ["title"],
    "Biome": ["displayName"],
    "COMET_ORBIT_TYPE": ["displayName", "description"],
    "Conclusion": True,
    "CONTROLPOINT": ["displayName"],
    "CREW_REQUEST": ["Keyword"],
    "EFFECT": ["effectDescription"],
    "EVASITUATION": ["resultString"],
    "EXPERIENCE_TRAIT": ["title", "desc"],
    "EXPERIMENT_DEFINITION": ["title"],
    "FIREWORKFX_DEFINITION": ["displayName", "color1Name", "color2Name", "color3Name"],
    "Introduction": True,
    "MESH": ["displayName"],
    "MISSIONPACK": ["displayName"],
    "MODULE": [
        "stagingEnableText", "stagingDisableText", "MeshMenuName", "NodeMenuName",
        "menuName", "startEventGUIName", "endEventGUIName", "actionGUIName",
        "defaultControlPointDisplayName", "baseDisplayName", "experimentActionName",
        "resetActionName", "reviewActionName", "storeActionName", "collectActionName",
        "toggleName", "eventOnName", "eventOffName", "extendpanelsActionName",
        "extendActionName", "retractActionName", "primaryEngineModeDisplayName",
        "secondaryEngineModeDisplayName", "ConverterName", "StartActionName",
        "StopActionName", "ToggleActionName", "resourceOutputName", "moduleType",
        "deployActionName", "displayseatName", "TooltipTitle", "TooltipPrimaryField",
        "switcherDescription", "switcherDescriptionPlural",
    ],
    "PARAM": ["Description"],
    "PART": ["title", "description", "manufacturer", "tags"],
    "PART_REQUEST": ["PartDescription", "VesselDescription", "Keyword"],
    "Problem": True,
    "Properties": ["displayName", "description"],
    "RESOURCE_DEFINITION": ["displayName", "abbreviation"],
    "RESULTS": True,
    "STRATEGY": ["title", "desc"],
    "STRATEGY_DEPARTMENT": ["title", "desc", "headName"],
    "SUBTYPE": ["title", "descriptionDetail", "descriptionSummary"],
    "SURVEY_DEFINITION": ["DataName", "AnomalyName", "ResultName"],
    "VARIANT": ["displayName", "description"],
    "VARIANTTHEME": ["displayName", "description"],
}

KOPERNICUS_BODY_FIELDS = {
    "Properties": ["displayName", "description"],
    "Biome": ["displayName"],
}

# 运行期收集的可疑情况，最后统一打印
WARNINGS = []

# ----------------------------------------------------------------------
# CFG 节点
# ----------------------------------------------------------------------


class CFGNode:
    """一个 cfg 节点：类型、选择器、:HAS 过滤器、其它标签、值、子节点。"""

    __slots__ = ("node_type", "selector", "filters", "conds", "values", "children")

    def __init__(self, node_type, selector="", filters=(), conds=()):
        self.node_type = node_type
        self.selector = selector
        self.filters = tuple(filters)
        self.conds = tuple(conds)
        self.values = OrderedDict()
        self.children = []

    def part(self):
        return self.node_type + self.selector

    def key_path(self):
        """用于生成 key 的路径片段，例如 Body:HAS[#name[Anger]]。

        头部没写选择器时，退回用节点里的 id / name 值做选择器，
        否则补丁会变成无选择器的全局节点。
        """
        ident = self.selector + "".join(self.filters)
        if not ident:
            for field in ("id", "name"):
                value = self.values.get(field)
                if value:
                    ident = ":HAS[#{}[{}]]".format(field, value)
                    break
        return self.node_type + ident


# ----------------------------------------------------------------------
# 词法：去注释 + 切分
# ----------------------------------------------------------------------


def strip_comments(text):
    """去掉 // 注释，但保留引号内、以及 http:// 这类 URL 里的 //。"""
    out = []
    i, n = 0, len(text)
    in_quote = False
    while i < n:
        ch = text[i]
        if in_quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
            i += 1
            continue
        if ch == '"':
            in_quote = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            # 只有位于行首或空白之后才算注释，避免吃掉 http://
            if i == 0 or text[i - 1] in " \t\r\n":
                while i < n and text[i] not in "\r\n":
                    i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def tokenize(text):
    """把 cfg 切成 ('line', 行) 与 ('brace', '{'/'}') 两类 token。"""
    tokens = []
    buf = []
    in_quote = False
    i, n = 0, len(text)

    def flush():
        if buf:
            line = "".join(buf).strip()
            if line:
                tokens.append(("line", line))
            buf.clear()

    while i < n:
        ch = text[i]
        if in_quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
            i += 1
            continue
        if ch == '"':
            in_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch in "{}":
            flush()
            tokens.append(("brace", ch))
            i += 1
            continue
        if ch in "\r\n":
            flush()
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return tokens


# ----------------------------------------------------------------------
# MM 语法：头部拆分
# ----------------------------------------------------------------------


def split_tags(s):
    """抽出所有 :TAG[...]（支持方括号嵌套，如 :HAS[@MODULE[KerbalEVA]]）。"""
    tags = []
    i = 0
    while True:
        j = s.find(":", i)
        if j < 0:
            break
        m = re.match(r":([A-Za-z_][A-Za-z0-9_]*)\[", s[j:])
        if not m:
            i = j + 1
            continue
        start = j + m.end()
        depth = 1
        k = start
        while k < len(s) and depth > 0:
            if s[k] == "[":
                depth += 1
            elif s[k] == "]":
                depth -= 1
            k += 1
        if depth != 0:
            break
        tags.append((m.group(1), s[start:k - 1]))
        s = s[:j] + s[k:]
        i = j
    return s, tags


def parse_header(header):
    """把 @Body:HAS[#name[Anger]] 拆成 (Body, '', (':HAS[...]',), ())。"""
    s = header.strip().rstrip(",").strip()
    s = re.sub(r"^[@+\-$%!^&*]+", "", s).strip()
    s, tags = split_tags(s)
    s = s.strip()

    filters, conds = [], []
    for tag, arg in tags:
        text = ":{}[{}]".format(tag, arg)
        if tag.upper() == "HAS":
            filters.append(text)
        else:
            conds.append(text)

    node_type, selector = "", ""
    m = re.match(r"^([^\s\[]+)\s*\[(.*)\]$", s, re.S)
    if m:
        node_type = m.group(1)
        selector = "[" + m.group(2) + "]"
    elif s:
        node_type = s.split()[0]
    return node_type, selector, tuple(filters), tuple(conds)


def normalize_value(value):
    """去掉值两端的引号并还原 \\" ；其余反斜杠（如 \\n）保持原样。"""
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].replace('\\"', '"')
    return value


# ----------------------------------------------------------------------
# 解析器
# ----------------------------------------------------------------------


def parse_cfg(filepath):
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read().lstrip("\ufeff")

    roots = []
    stack = []
    pending = None

    for kind, value in tokenize(strip_comments(raw)):
        if kind == "brace":
            if value == "{":
                node = CFGNode(*parse_header(pending or ""))
                if not node.node_type:
                    WARNINGS.append("{}: 无法识别的节点头 {!r}".format(filepath, pending))
                if stack:
                    stack[-1].children.append(node)
                else:
                    roots.append(node)
                stack.append(node)
            else:
                if stack:
                    stack.pop()
                else:
                    WARNINGS.append("{}: 出现多余的 }}".format(filepath))
            pending = None
            continue

        line = value.strip()
        if not line:
            continue
        if "=" in line and stack:
            key, val = line.split("=", 1)
            key = re.sub(r"^[@+\-$%!^&*]+", "", key.strip()).strip()
            if key:
                stack[-1].values[key] = normalize_value(val)
            pending = None
        elif "=" in line:
            WARNINGS.append("{}: 节点外的赋值 {!r}（已忽略）".format(filepath, line))
        else:
            pending = line

    return roots


def walk_nodes(nodes, path=""):
    for node in nodes:
        node_path = node.key_path() if not path else "{}/{}".format(path, node.key_path())
        yield node, node_path
        for item in walk_nodes(node.children, node_path):
            yield item


# ----------------------------------------------------------------------
# 本地化键
# ----------------------------------------------------------------------


def generate_localization_key(mod_name, unique_id, field_name):
    unique_str = "{}:{}".format(unique_id, field_name)
    hash_hex = hashlib.md5(unique_str.encode("utf-8")).hexdigest()[:8].upper()
    return "#autoLOC_{}_{}".format(mod_name, hash_hex)


# ----------------------------------------------------------------------
# 提取
# ----------------------------------------------------------------------


def extract_from_directory(root_dir, mod_name="BeyondHome", debug=False):
    root_dir_abs = os.path.abspath(root_dir)
    entries = []
    dictionary = OrderedDict()

    all_node_fields = {}
    for node_type, fields in LOCALIZABLE_FIELDS.items():
        all_node_fields[node_type.upper()] = fields
    for node_type, fields in KOPERNICUS_BODY_FIELDS.items():
        upper = node_type.upper()
        if upper in all_node_fields:
            if isinstance(all_node_fields[upper], list) and isinstance(fields, list):
                all_node_fields[upper] = list(set(all_node_fields[upper] + fields))
        else:
            all_node_fields[upper] = fields

    cfg_files = []
    for dirpath, _, filenames in os.walk(root_dir):
        for name in filenames:
            if name.lower().endswith(".cfg"):
                cfg_files.append(os.path.join(dirpath, name))
    print("找到 {} 个 .cfg 文件".format(len(cfg_files)))
    if not cfg_files:
        return [], {}

    for filepath in cfg_files:
        try:
            nodes = parse_cfg(filepath)
        except Exception as exc:  # noqa: BLE001
            WARNINGS.append("{}: 解析失败 {}".format(filepath, exc))
            continue
        rel_path = os.path.relpath(filepath, root_dir_abs)

        for node, node_path in walk_nodes(nodes):
            if debug:
                print("[DEBUG] {}  文件: {}".format(node_path, rel_path))

            node_type_upper = node.node_type.upper()
            values_lower = {k.lower(): k for k in node.values}
            cond = "".join(node.conds)

            allowed = all_node_fields.get(node_type_upper)
            if allowed is True:
                fields_to_extract = list(node.values.keys())
            elif isinstance(allowed, list):
                fields_to_extract = allowed
            else:
                fields_to_extract = []

            for field in fields_to_extract:
                original_key = values_lower.get(field.lower())
                if not original_key:
                    continue
                original_value = node.values[original_key]
                if not original_value or original_value.startswith("#autoLOC_"):
                    continue
                loc_key = generate_localization_key(mod_name, node_path, field)
                entries.append((node_path, field, original_value, loc_key, cond, rel_path))
                dictionary[loc_key] = original_value

            # Kopernicus 的 Body 结构：Properties / Biomes 需要单独处理
            if node_type_upper == "BODY":
                for child in node.children:
                    child_upper = child.node_type.upper()
                    child_path = "{}/{}".format(node_path, child.key_path())
                    if child_upper == "PROPERTIES":
                        child_values = {k.lower(): k for k in child.values}
                        for field in ("displayName", "description"):
                            original_key = child_values.get(field.lower())
                            if not original_key:
                                continue
                            value = child.values[original_key]
                            if not value or value.startswith("#autoLOC_"):
                                continue
                            loc_key = generate_localization_key(mod_name, child_path, field)
                            entries.append((child_path, field, value, loc_key,
                                            "".join(child.conds), rel_path))
                            dictionary[loc_key] = value
                    elif child_upper == "BIOMES":
                        for biome in child.children:
                            if biome.node_type.upper() != "BIOME":
                                continue
                            biome_path = "{}/{}".format(child_path, biome.key_path())
                            biome_values = {k.lower(): k for k in biome.values}
                            for field in ("displayName",):
                                original_key = biome_values.get(field.lower())
                                if not original_key:
                                    continue
                                value = biome.values[original_key]
                                if not value or value.startswith("#autoLOC_"):
                                    continue
                                loc_key = generate_localization_key(mod_name, biome_path, field)
                                entries.append((biome_path, field, value, loc_key,
                                                "".join(biome.conds), rel_path))
                                dictionary[loc_key] = value

    return entries, dictionary


# ----------------------------------------------------------------------
# 生成补丁
# ----------------------------------------------------------------------


def split_path_parts(path):
    """按 / 切分节点路径，但忽略方括号里的 /。"""
    parts, depth, buf = [], 0, []
    for ch in path:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "/" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p]


def hoist_bodies(parts, full_path):
    """Kopernicus 的 Body 必须是 Kopernicus 的直接子节点。"""
    body_idx = [i for i, p in enumerate(parts) if p[0].lower() == "body"]
    if not body_idx:
        return parts

    if len(body_idx) > 1:
        WARNINGS.append("Body 嵌套（已提升到 Kopernicus 下）：{}".format(full_path))
        for i in reversed(body_idx[:-1]):
            del parts[i]
        body_idx = [i for i, p in enumerate(parts) if p[0].lower() == "body"]

    node_type, selector, filters, _ = parts[body_idx[0]]
    if not selector and not filters:
        WARNINGS.append("跳过没有选择器的 Body 补丁（否则会套到所有天体）：{}".format(full_path))
        return None
    return parts


def generate_mm_patch(entries, output_path, mod_name="BeyondHome"):
    tree = OrderedDict()

    for full_path, field, _value, loc_key, _cond, _rel in entries:
        parts = [parse_header(p) for p in split_path_parts(full_path)]
        parts = [p for p in parts if p[0]]
        if not parts:
            continue
        if parts[0][0].lower() == "kopernicus":
            parts = hoist_bodies(parts, full_path)
            if parts is None:
                continue

        current = tree
        for index, (node_type, selector, filters, _conds) in enumerate(parts):
            key = (node_type, selector, filters)
            if key not in current:
                current[key] = {
                    "fields": [],
                    "children": OrderedDict(),
                    # Kopernicus 本来就是单例根节点，不算“全局补丁”
                    "global": (index == 0 and not selector and not filters
                               and node_type.lower() != "kopernicus"),
                }
            if index == len(parts) - 1:
                pair = (field, loc_key)
                if pair not in current[key]["fields"]:
                    current[key]["fields"].append(pair)
            else:
                current = current[key]["children"]

    def write_level(handle, level, indent):
        pad = "    " * indent
        for (node_type, selector, filters), data in level.items():
            header = "@{}{}{}".format(node_type, selector, "".join(filters))
            if indent == 0:
                if ":NEEDS[" not in header and ":FOR[" not in header:
                    header += ":NEEDS[{}]".format(mod_name)
                # 排在最后，确保在 BeyondHome 等模组挂完节点之后再生效
                header += ":FINAL"
            if data["global"]:
                handle.write("{}// 警告：该节点没有选择器，会对全游戏所有同名节点生效\n".format(pad))
            handle.write("{}{}\n{}{{\n".format(pad, header, pad))
            for field, loc_key in sorted(data["fields"]):
                handle.write("{}    @{} = {}\n".format(pad, field, loc_key))
            if data["children"]:
                write_level(handle, data["children"], indent + 1)
            handle.write("{}}}\n".format(pad))
            if indent == 0:
                handle.write("\n")

    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write("// 自动生成的 {} 本地化 MM 补丁\n".format(mod_name))
        handle.write("// 请勿手动编辑，翻译请修改对应的字典文件\n\n")
        write_level(handle, tree, 0)

    print("MM 补丁已生成：{}".format(output_path))


# ----------------------------------------------------------------------
# 生成字典
# ----------------------------------------------------------------------


def generate_dictionary(entries, output_path):
    loc_map = OrderedDict()
    for full_path, field, original_value, loc_key, _cond, rel_path in entries:
        if loc_key in loc_map:
            continue
        comment = "Source: {} | Node: {} | Field: {}".format(rel_path, full_path, field)
        loc_map[loc_key] = (original_value, comment)

    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write("// 本地化字典（英文原文）\n")
        handle.write("// 请翻译下方文本，并保存为对应语言文件（如 zh-cn）\n\n")
        handle.write("Localization\n{\n    en-us\n    {\n")
        for loc_key, (value, comment) in loc_map.items():
            safe = (
                value.replace("\r", "")
                .replace("\n", "\\n")
                .replace("\\", "\\\\")  # 先转义反斜杠，避免 \\n 被吃掉
                .replace('"', '\\"')
            )
            handle.write("        // {}\n".format(comment))
            handle.write('        {} = "{}"\n\n'.format(loc_key, safe))
        handle.write("    }\n}\n")

    print("本地化字典已生成：{}".format(output_path))


# ----------------------------------------------------------------------
# 主程序
# ----------------------------------------------------------------------


def main():
    args = list(sys.argv[1:])
    debug_mode = "--debug" in args
    args = [a for a in args if a != "--debug"]

    out_dir = None
    if "--out" in args:
        index = args.index("--out")
        if index + 1 >= len(args):
            print("--out 需要一个目录参数")
            sys.exit(1)
        out_dir = args[index + 1]
        del args[index:index + 2]

    if not args:
        print(__doc__)
        sys.exit(1)

    root_dir = args[0]
    mod_name = args[1] if len(args) > 1 else "BeyondHome"
    if not os.path.isdir(root_dir):
        print("错误：路径 '{}' 不存在。".format(root_dir))
        sys.exit(1)

    if out_dir is None:
        candidate = os.path.join("GameData", "SaveRhode", mod_name)
        out_dir = candidate if os.path.isdir(candidate) else "."
    os.makedirs(out_dir, exist_ok=True)

    print("正在扫描：{}".format(root_dir))
    entries, _ = extract_from_directory(root_dir, mod_name, debug=debug_mode)
    if not entries:
        print("未找到可本地化字段。")
        return

    print("找到 {} 个字段。".format(len(entries)))

    patch_file = os.path.join(out_dir, "{}_Localization_Patch.cfg".format(mod_name))
    dict_file = os.path.join(out_dir, "{}_Localization_Dictionary.cfg".format(mod_name))
    generate_mm_patch(entries, patch_file, mod_name)
    generate_dictionary(entries, dict_file)

    if WARNINGS:
        print("\n需要注意的 {} 处：".format(len(WARNINGS)))
        for item in dict.fromkeys(WARNINGS):
            print("  - {}".format(item))
    print("完成！")


if __name__ == "__main__":
    main()
