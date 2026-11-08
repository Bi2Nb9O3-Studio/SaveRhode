# LuaSapling → `.mu` 导出说明

Parallax 的散点模型只能加载 Unity 序列化出来的 `.mu`（`GameObject`），
不能直接在运行时读取 `.obj`。本目录里的 `.obj` 是**源资产**，需要走一次
Unity + PartTools 的导出流程，生成同名 `.mu` 后配置才会生效。

## 目录内容

| 文件 | 用途 |
|---|---|
| `saplingTrunkLOD0.obj` | 树干/枝条 高模（8,555 三角面） |
| `saplingTrunkLOD1.obj` | 中模（1,326 面） |
| `saplingTrunkLOD2.obj` | 公告板单片（2 面，pivot 在根部） |
| `saplingTrunkLOD0.mtl` | 材质占位（Unity 可不导入） |
| `saplingCanopyLOD0.obj` | 树冠叶簇 高模（700 面） |
| `saplingCanopyLOD1.obj` | 树冠 低模（252 面） |

贴图在 `GameData/SaveRhode/Textures/LuaSapling/`，是 Parallax 运行时直接读取的
DDS（DXT5 + 完整 mipmap），**不需要**导入 Unity。

## 一次性导出步骤

1. **环境**：Unity 2019.4.x（KSP 1.12 对应版本）+ KSP PartTools
   （<https://github.com/KSPModdingLibs/PartTools> 或 CKAN 里的 PartTools）。
2. 新建 3D 项目，把 PartTools 的 `Editor` 目录放进 `Assets/`。
3. 把 5 个 `.obj` 拖进 `Assets/`，导入设置保持默认（Scale Factor = 1，
   Y 轴向上，单位米）。
4. 每个 OBJ 在场景里确认：**原点在树干根部**、朝向 +Z、高度约 2.85 m。
   如果导入后模型躺倒，把 Import 设置里的 `Convert Axis` 改为 Y-Up。
5. 给每个物体挂一个带 `MeshRenderer` 的空材质（Parallax 会在运行时替换材质，
   这里只是让 PartTools 有东西可导出）。
6. 菜单 `Window → Part Tools`：
   - `Model Export` 选到
     `<KSP>/GameData/SaveRhode/Models/LuaSapling/`
   - 依次选中物体 → 点 **Write**，导出的文件名为物体名 + `.mu`
7. 导出完成后目录里应出现：

```
saplingTrunkLOD0.mu
saplingTrunkLOD1.mu
saplingTrunkLOD2.mu
saplingCanopyLOD0.mu
saplingCanopyLOD1.mu
```

名字必须与上表完全一致，`LuaSaplings.cfg` 里的 `model = ` 路径就是按它们写的。

## 验证

进游戏后看 `KSP.log`：

- 正常：没有 `Model ... not found` 之类的报错，Lua 低地上出现稀疏树苗；
- 若出现找不到模型，检查 `.mu` 是否和 `.cfg` 里的路径大小写一致
  （KSP 在 Windows 上大小写不敏感，但换到 Linux 会敏感）。

## 提示

- 重新生成模型：`python tools/make_lua_sapling.py`，会覆盖本目录的 `.obj`
  和贴图；导出的 `.mu` 需要重新导出一次。
- LOD 面数：0 → 8,555 / 1,326 / 2（树干），700 / 252（树冠）。
  如果你的目标机器较弱，可以在生成器里把 `sides` / `seg` 调小。
- 模型没有内置碰撞体；`collisionLevel = 2` 由 Parallax 用网格近似生成。
