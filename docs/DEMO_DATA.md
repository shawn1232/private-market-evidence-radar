# Synthetic Demo｜合成演示

演示数据只用于验证界面、状态机和 JSON 契约，不用于测试真实搜索覆盖或投资判断。

## 载入

```bash
python scripts/load_demo.py
```

脚本读取：

- `examples/synthetic_weekly_report.json`
- `examples/synthetic_deep_report.json`

并写入：

- `data/output/weekly_radar.json`
- `data/output/latest_report.json`

所有输出都包含：

```json
{
  "synthetic": true,
  "demo_notice": "SYNTHETIC DEMO DATA - NOT A REAL COMPANY, SOURCE, OR INVESTMENT RECOMMENDATION"
}
```

公司、公众号、事件和引文均为人工编写的虚构内容；URL 使用 `.invalid` 保留域名。脚本不会联网，也不会读取 `data/raw/`、`sessions/` 或现有文章库。

## 覆盖保护

若目标 JSON 已存在且没有 `synthetic: true`，脚本默认拒绝覆盖：

```text
Refusing to overwrite non-synthetic output ...
```

只有明确知道目标是可丢弃的本地演示环境时，才使用：

```bash
python scripts/load_demo.py --force
```

`--force` 仍只会写入上述两个 `data/output` 文件，内容仍然是合成数据。

## 演示建议

1. 打开周度雷达，解释“数据截至”和“待核实”状态。
2. 展开候选卡片，指出合成引文与 `.invalid` URL。
3. 点击“带入深度评估”，说明深评工作台会隔离带有演示标记的报告，避免将 fixture 当成真实结论。
4. 运行测试，展示边界测试而非依赖实时网络结果。
