# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架。

过期下架在全层页先干跑（GET /api/expire-sweep，只读）预览名单，再提交
（POST /api/expire-sweep）：提交在单事务内按提交瞬间重算，干跑后新入库的
过期批一并带走、期间被消费的批跳过，未到期批不动，重复提交幂等；中途失败
全层/层页/顶条整体回到提交前。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest` / `temp_zone`。
