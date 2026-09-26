# task26092717

供应商名录 CSV 导入导出模块（纯标准库，SQLite 存储）。

## 用法

```python
from suppliers import SupplierDirectory

d = SupplierDirectory("suppliers.db")   # 或 ":memory:"
report = d.import_csv(owner_id, data)   # data: bytes，整个文件原子生效
blob = d.export_csv(owner_id)           # bytes，UTF-8 带 BOM，Excel 可直接打开
suppliers = d.list_suppliers(owner_id)
```

`Report` 含 `ok`、`errors`（每条带记录起始物理行号与列名，表头为第 1 行）、
`created`、`updated`（字段无变化的行不计入）。

## 格式约定

- 表头列名：`id, company_name, contact_person, phone, street_address,
  lead_time_days, moq, reliability, categories`。导入时列顺序任意、
  大小写与首尾空白不敏感；`id` 列可省略；BOM / CRLF / LF / CR 均可。
- `phone`：接受 `(415) 555-1234`、`415-555-1234`、`415.555.1234`、`+1 ...`
  等常见写法，统一存为 `(NXX) NXX-XXXX`。
- `categories`：同一单元格内是一条**内嵌 CSV 行**（逗号分隔，必要时按
  CSV 规则加引号），因此品类名可含逗号、引号、换行等任意字符；
  至多 20 个非空品类，同一供应商内忽略大小写去重。
- 防公式注入：导出时，凡以 `=`、`+`、`-`、`@`、制表符、回车或 `'`
  开头的文本单元格，前面加一个 `'`；导入时若 `'` 后紧跟上述字符或
  另一个 `'`，则去掉这一个 `'`。往返完全可逆，不拒收任何危险字符。
- `company_name` 在同一 owner 下唯一：比较时忽略大小写、Unicode 兼容
  形式（NFKC）与所有空白差异。唯一性按导入后的最终状态判定，
  同一文件内两家供应商互换名字可以成功。
- 不带 `id` 的行按 `company_name` 匹配导入前的状态，命中则更新，
  否则新建；文件里没出现的供应商不动。数据行上限 500，空行不计。

## 测试

```
python3 -m unittest test_suppliers -v
```
