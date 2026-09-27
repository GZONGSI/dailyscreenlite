# 生成的契约产物

目录里只有下面两个文件是**生成物**（本说明文件是手工维护的）：

- `openapi.json`：由后端 DTO 隔离导出，`backend/tools/export_openapi.py` 写入。
- `schema.d.ts`：由 `openapi-typescript` 从上面的 schema 生成。

两者都是**生成物**：不要手改，改了下次生成就丢。业务类型与方法按功能引用生成结果
（当前：[`../notes.ts`](../notes.ts) 笔记、[`../classification.ts`](../classification.ts)
候选归类与观察联动、[`../observations.ts`](../observations.ts) 观察工作表、
[`../securities.ts`](../securities.ts) 证券身份，各自只给生成类型起短名字）。

## 重新生成

两条命令分别在仓库根目录与 `frontend/` 下执行：

```powershell
# 仓库根目录：后端 DTO → openapi.json
backend\.venv\Scripts\python.exe -X utf8 backend\tools\export_openapi.py

# frontend 目录：openapi.json → schema.d.ts
cd frontend
pnpm run gen:api
```

## 检查是否过期（重新生成无差异）

```powershell
# 仓库根目录：schema 是否与当前 DTO 一致
backend\.venv\Scripts\python.exe -X utf8 backend\tools\export_openapi.py --check

# frontend 目录：schema.d.ts 是否与 schema 一致
cd frontend
pnpm run check:api
```

导出是隔离且确定性的：临时数据目录、交付证券库、空行情夹具、不启调度与问财，
只导出 `/api` 路径、键排序、不含时间戳。所以同一份后端模型重复导出必须逐字一致。
