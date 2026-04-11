#!/usr/bin/env python3
"""
KSP CFG 可本地化字段提取与 MM 补丁生成器
用法：
    python extract_localizable.py /path/to/BeyondHome/GameData/BeyondHome [--debug]
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

# ----------------------------------------------------------------------
# CFG 节点类
# ----------------------------------------------------------------------


class CFGNode:
    def __init__(self, node_type, name="", mm_condition=""):
        self.node_type = node_type
        self.name = name
        self.mm_condition = mm_condition
        self.values = OrderedDict()
        self.children = []

    def add_value(self, key, value):
        self.values[key] = value

    def add_child(self, node):
        self.children.append(node)

    def get_path(self):
        return self.node_type

    def get_identifier(self):
        if self.name:
            return f"[{self.name}]"
        if 'id' in self.values:
            return f":HAS[#id[{self.values['id']}]]"
        if 'name' in self.values:
            return f":HAS[#name[{self.values['name']}]]"
        return ""

# ----------------------------------------------------------------------
# MM 语法处理
# ----------------------------------------------------------------------


def strip_mm_prefix_and_condition(header):
    original = header.strip()
    mm_condition = ""
    cond_match = re.search(
        r':(NEEDS|FOR|AFTER|BEFORE|FIRST|LAST|FINAL)\[.*?\]', original)
    if cond_match:
        mm_condition = cond_match.group(0)
        header = original.replace(mm_condition, "").strip()
    else:
        header = original

    clean_header = re.sub(r'^[@+\-%!]+', '', header)
    clean_header = re.sub(r',\*', '', clean_header)
    parts = clean_header.split(None, 1)
    node_type = parts[0] if parts else ""
    node_name = ""
    if len(parts) > 1:
        node_name = parts[1]
        if node_name.startswith('[') and node_name.endswith(']'):
            node_name = node_name[1:-1]
    return node_type, node_name, mm_condition

# ----------------------------------------------------------------------
# 解析器
# ----------------------------------------------------------------------


def parse_cfg(filepath):
    root_nodes = []
    stack = []
    pending_header = None
    pending_condition = ""

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith('//'):
                continue
            if '//' in line:
                line = line.split('//')[0].strip()
            if not line:
                continue

            if '{' in line:
                header = line.split('{')[0].strip()
                if not header:
                    if pending_header is not None:
                        header = pending_header
                        mm_cond = pending_condition
                        pending_header = None
                        pending_condition = ""
                    else:
                        continue
                else:
                    mm_cond = ""
                    cond_match = re.search(
                        r':(NEEDS|FOR|AFTER|BEFORE|FIRST|LAST|FINAL)\[.*?\]', header)
                    if cond_match:
                        mm_cond = cond_match.group(0)
                        header = header.replace(mm_cond, "").strip()
                    pending_header = None
                    pending_condition = ""

                node_type, node_name, _ = strip_mm_prefix_and_condition(header)
                if not node_type:
                    continue
                new_node = CFGNode(node_type, node_name, mm_cond)
                if stack:
                    stack[-1].add_child(new_node)
                else:
                    root_nodes.append(new_node)
                stack.append(new_node)

            elif '}' in line:
                if stack:
                    stack.pop()
                pending_header = None
                pending_condition = ""

            else:
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    clean_key = re.sub(r'^[@+\-%!]+', '', key)
                    if stack:
                        stack[-1].add_value(clean_key, value)
                    pending_header = None
                else:
                    pending_header = line
                    cond_match = re.search(
                        r':(NEEDS|FOR|AFTER|BEFORE|FIRST|LAST|FINAL)\[.*?\]', line)
                    if cond_match:
                        pending_condition = cond_match.group(0)
                        pending_header = line.replace(
                            pending_condition, "").strip()
                    else:
                        pending_condition = ""
    return root_nodes


def walk_nodes(nodes, callback, path_prefix=""):
    for node in nodes:
        full_path = node.get_path(
        ) if not path_prefix else f"{path_prefix}/{node.get_path()}"
        callback(node, full_path)
        if node.children:
            walk_nodes(node.children, callback, full_path)

# ----------------------------------------------------------------------
# 本地化键生成
# ----------------------------------------------------------------------


def generate_localization_key(mod_name, unique_id, field_name):
    unique_str = f"{unique_id}:{field_name}"
    hash_hex = hashlib.md5(unique_str.encode('utf-8')).hexdigest()[:8].upper()
    return f"#autoLOC_{mod_name}_{hash_hex}"

# ----------------------------------------------------------------------
# 提取函数
# ----------------------------------------------------------------------


def extract_from_directory(root_dir, mod_name="BeyondHome", debug=False):
    root_dir_abs = os.path.abspath(root_dir)
    # (full_node_path, field, original_value, loc_key, mm_cond, rel_path)
    localizable_entries = []
    dictionary_entries = OrderedDict()

    all_node_fields = {}
    for node_type, fields in LOCALIZABLE_FIELDS.items():
        all_node_fields[node_type.upper()] = fields
    for node_type, fields in KOPERNICUS_BODY_FIELDS.items():
        upper = node_type.upper()
        if upper in all_node_fields:
            if isinstance(all_node_fields[upper], list) and isinstance(fields, list):
                all_node_fields[upper] = list(
                    set(all_node_fields[upper] + fields))
        else:
            all_node_fields[upper] = fields

    cfg_files = []
    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            if f.lower().endswith('.cfg'):
                cfg_files.append(os.path.join(dirpath, f))
    print(f"找到 {len(cfg_files)} 个 .cfg 文件")

    if not cfg_files:
        return [], {}

    for full_path in cfg_files:
        try:
            nodes = parse_cfg(full_path)
        except Exception as e:
            print(f"警告：解析文件失败 {full_path}: {e}")
            continue

        try:
            rel_path = os.path.relpath(full_path, root_dir_abs)
        except ValueError:
            rel_path = full_path

        def process_node(node, node_path):
            if debug:
                identifier = node.get_identifier()
                print(
                    f"[DEBUG] 节点: {node.node_type}{identifier}  文件: {rel_path}")

            node_type_upper = node.node_type.upper()
            node_fields_lower = {k.lower(): k for k in node.values.keys()}
            identifier = node.get_identifier()
            full_node_path = f"{node_path}{identifier}"
            mm_cond = node.mm_condition

            allowed_fields = all_node_fields.get(node_type_upper)
            if allowed_fields is True:
                fields_to_extract = list(node.values.keys())
            elif isinstance(allowed_fields, list):
                fields_to_extract = allowed_fields
            else:
                fields_to_extract = []

            for field in fields_to_extract:
                field_lower = field.lower()
                if field_lower in node_fields_lower:
                    original_key = node_fields_lower[field_lower]
                    original_value = node.values[original_key]
                    if not original_value or original_value.startswith('#autoLOC_'):
                        continue
                    loc_key = generate_localization_key(
                        mod_name, full_node_path, field)
                    localizable_entries.append(
                        (full_node_path, field, original_value,
                         loc_key, mm_cond, rel_path)
                    )
                    dictionary_entries[loc_key] = original_value

            if node_type_upper == "BODY":
                for child in node.children:
                    child_upper = child.node_type.upper()
                    child_id = child.get_identifier()
                    child_full_path = f"{full_node_path}/{child.get_path()}{child_id}"
                    if child_upper == "PROPERTIES":
                        child_fields_lower = {
                            k.lower(): k for k in child.values.keys()}
                        for field in ["displayName", "description"]:
                            if field.lower() in child_fields_lower:
                                orig_key = child_fields_lower[field.lower()]
                                val = child.values[orig_key]
                                if val and not val.startswith('#autoLOC_'):
                                    loc_key = generate_localization_key(
                                        mod_name, child_full_path, field)
                                    localizable_entries.append(
                                        (child_full_path, field, val, loc_key,
                                         child.mm_condition, rel_path)
                                    )
                                    dictionary_entries[loc_key] = val
                    elif child_upper == "BIOMES":
                        for biome_child in child.children:
                            if biome_child.node_type.upper() == "BIOME":
                                biome_id = biome_child.get_identifier()
                                biome_full_path = f"{child_full_path}/{biome_child.get_path()}{biome_id}"
                                biome_fields_lower = {
                                    k.lower(): k for k in biome_child.values.keys()}
                                for field in ["displayName"]:
                                    if field.lower() in biome_fields_lower:
                                        orig_key = biome_fields_lower[field.lower(
                                        )]
                                        val = biome_child.values[orig_key]
                                        if val and not val.startswith('#autoLOC_'):
                                            loc_key = generate_localization_key(
                                                mod_name, biome_full_path, field)
                                            localizable_entries.append(
                                                (biome_full_path, field, val, loc_key,
                                                 biome_child.mm_condition, rel_path)
                                            )
                                            dictionary_entries[loc_key] = val

        walk_nodes(nodes, process_node)

    return localizable_entries, dictionary_entries

# ----------------------------------------------------------------------
# 生成补丁
# ----------------------------------------------------------------------


def generate_mm_patch(entries, output_path, mod_name="BeyondHome"):
    tree = {}

    for full_path, field, _, loc_key, mm_cond, _ in entries:
        parts = full_path.split('/')
        current_level = tree
        for i, part in enumerate(parts):
            if ':' in part and ':HAS[' in part:
                colon_idx = part.index(':')
                node_type = part[:colon_idx]
                selector = part[colon_idx:]
            elif '[' in part and part.endswith(']'):
                bracket_idx = part.index('[')
                node_type = part[:bracket_idx]
                selector = part[bracket_idx:]
            else:
                node_type = part
                selector = ""
            key = (node_type, selector)
            if key not in current_level:
                current_level[key] = {
                    "fields": set(), "children": {}, "condition": mm_cond}
            if i == len(parts) - 1:
                current_level[key]["fields"].add((field, loc_key))
            else:
                current_level = current_level[key]["children"]

    def write_node(f, level_dict, indent=0):
        prefix = "    " * indent
        for (node_type, selector), data in level_dict.items():
            condition = data.get("condition", "")
            if indent == 0 and ":NEEDS" not in condition and ":FOR" not in condition:
                condition = f":NEEDS[{mod_name}]"
            header = f"@{node_type}{selector}{condition}"
            f.write(f"{prefix}{header}\n")
            f.write(f"{prefix}{{\n")
            for field, loc_key in sorted(data["fields"]):
                f.write(f"{prefix}    @{field} = {loc_key}\n")
            if data["children"]:
                write_node(f, data["children"], indent + 1)
            f.write(f"{prefix}}}\n")

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("// 自动生成的 BeyondHome 本地化 MM 补丁\n")
        f.write("// 请勿手动编辑，翻译请修改对应的字典文件\n\n")
        write_node(f, tree)
        f.write("\n")
    print(f"MM 补丁已生成：{output_path}")

# ----------------------------------------------------------------------
# 生成字典（带注释路径）
# ----------------------------------------------------------------------


def generate_dictionary(entries, output_path):
    # 构建去重的 loc_key -> (value, comment) 映射
    loc_map = OrderedDict()
    for full_node_path, field, original_value, loc_key, mm_cond, rel_path in entries:
        comment = f"Source: {rel_path} | Node: {full_node_path} | Field: {field}"
        if loc_key not in loc_map:
            loc_map[loc_key] = (original_value, comment)
        # 如果同一个键出现多次（理论上不会），忽略后续

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("// BeyondHome 本地化字典（英文原文）\n")
        f.write("// 请翻译下方文本，并保存为对应语言文件（如 zh-cn）\n\n")
        f.write("Localization\n{\n    en-us\n    {\n")
        for loc_key, (value, comment) in loc_map.items():
            f.write(f"        // {comment}\n")
            safe_value = value.replace('"', '\\"')
            f.write(f'        {loc_key} = "{safe_value}"\n\n')
        f.write("    }\n}\n")
    print(f"本地化字典已生成：{output_path}")

# ----------------------------------------------------------------------
# 主程序
# ----------------------------------------------------------------------


def main():
    debug_mode = False
    args = sys.argv[1:]
    if '--debug' in args:
        debug_mode = True
        args.remove('--debug')
    if len(args) < 1:
        print(
            "用法：python extract_localizable.py <BeyondHome文件夹路径> [模组名称] [--debug]")
        sys.exit(1)
    root_dir = args[0]
    mod_name = args[1] if len(args) > 1 else "BeyondHome"

    if not os.path.isdir(root_dir):
        print(f"错误：路径 '{root_dir}' 不存在。")
        sys.exit(1)

    print(f"正在扫描：{root_dir}")
    entries, _ = extract_from_directory(root_dir, mod_name, debug=debug_mode)

    if not entries:
        print("未找到可本地化字段。")
        return

    print(f"找到 {len(entries)} 个字段。")

    patch_file = f"{mod_name}_Localization_Patch.cfg"
    dict_file = f"{mod_name}_Localization_Dictionary.cfg"
    generate_mm_patch(entries, patch_file, mod_name)
    generate_dictionary(entries, dict_file)

    print("完成！")


if __name__ == "__main__":
    main()
