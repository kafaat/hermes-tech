# مواصفة واجهة الأوامر `hermes` · الإصدار 1.0

الواجهة أداة المؤسس والفريق التقني. كل أمر:
- يتطلب جلسة مشغّل (`app.operators`) مع توثيق متعدد العوامل؛
- يمر عبر طبقة الفرض نفسها (لا مسار جانبي)؛
- يكتب حدثًا في `app.audit_log` باسم الحدث المذكور؛
- يعيد رمز خروج: `0` نجاح · `2` مدخلات غير صالحة · `3` مرفوض بالصلاحية · `4` غير موجود · `5` تعارض حالة · `10` خطأ خادم.

| الأمر | الوسائط | الأثر | الدور الأدنى | حدث التدقيق |
|---|---|---|---|---|
| `agent status` | `[--agent ID] [--customer CUST]` | حالة الدائرة والإنفاق والنداءات | ops | — |
| `agent health` | — | ملخص صحة كل الوكلاء | ops | — |
| `agent disable` | `--agent ID [--customer CUST] --reason INC` | إيقاف وكيل عمومًا أو لعميل | ops | `agent.disabled` |
| `agent enable` | `--agent ID [--customer CUST]` | إعادة التفعيل | ops | `agent.enabled` |
| `agent reset-circuit` | `--agent ID [--customer CUST]` | إغلاق الدائرة يدويًا | tech | `agent.circuit_reset` |
| `calls` | `[--agent] [--customer] [--since 24h] [--outcome X\|!X] [--limit N]` | قراءة سجل النداءات | ops | — |
| `cost` | `[--customer] [--since 30d] [--group-by customer\|agent] [--breakdown agent]` | تقارير التكلفة | ops | — |
| `queue breakdown` | — | عمق الطابور حسب الوكيل والأولوية | ops | — |
| `campaign pause` / `resume` | `--campaign ID` | إيقاف حملة جماعية أو استئنافها | ops | `campaign.paused` |
| `worker add` / `remove` | `--role ROLE` | تغيير عدد العمال | tech | `worker.scaled` |
| `deploy` | `--ref REF --canary 5\|25\|100` | نشر المنسّق أو القوالب تدريجيًا | tech | `deploy.started` |
| `rollback` | `--customer CUST --to VERSION` أو `--group GROUP --to VERSION` | رجوع موقع أو مجموعة | tech | `site.rolled_back` |
| `freeze` / `unfreeze` | `--customer CUST` | إيقاف كل الوكلاء لعميل | ops | `customer.frozen` |
| `secrets revoke` | `--customer CUST --all` | إلغاء مفاتيح العميل في مخزن الأسرار | founder | `secrets.revoked` |
| `secrets rotate` | `--global` | تدوير الأسرار العامة | founder | `secrets.rotated` |
| `sessions terminate` | `--customer CUST` | إنهاء جلسات البوابة | ops | `sessions.terminated` |
| `logs export` | `--customer CUST --since 72h --to PATH` | حفظ الأدلة | founder | `logs.exported` |
| `approvals list` | `[--customer] [--pending]` | عرض المقترحات | ops | — |
| `approvals execute` | `--id APPROVAL` | تنفيذ مقترح موافق عليه (نشر، إرسال، تفعيل) | ops | `approval.executed` |
| `outbox attention` | — | صفوف الصندوق الصادر التي تنتظر إنسانًا (v_outbox_attention): غامضة، أو تجاوزت المحاولات، أو بلا تحديث | ops | — |
| `outbox resolve` | `--id OUTBOX --confirmed-sent\|--resend\|--abandon --reason TEXT` | حسم إرسال غامض بجلسة aal2 وسبب مكتوب (app.resolve_outbox، 1.8)؛ لا حسم دون فحص سجل المزوّد أولًا | ops | `outbox.resolved` |
| `channels list` | `[--customer CUST]` | عرض قنوات العميل الرسمية وحالتها | ops | — |
| `channels verify` | `--id CHANNEL` | تأكيد قناة بعد تحقق Meta وتفعيلها | ops | `channel.verified` |
| `flags list` | `[--customer] [--open]` | عرض أعلام content_guard وagent_quality | ops | — |
| `flags resolve` | `--id FLAG --status confirmed\|dismissed` | حسم علم جودة | ops | `flag.resolved` |
| `evals run` | `--suite complaints\|content [--strict]` | تشغيل مجموعات التقييم وتسجيلها في app.eval_runs | tech | `eval.recorded` |
| `incident open` / `update` / `close` | `--severity --category` / `ID --field value` / `ID` | إدارة الحادث في `app.incidents` | ops | `incident.*` |
| `gates record` | `--gate ID --value X --source S` | تسجيل قياس بوابة في `app.gate_measurements` | founder | `gate.recorded` |

قواعد:
1. `approvals execute` يرفض أي مقترح قراره ليس `approved` أو انتهت صلاحيته (تفرضه أيضًا حراسات قاعدة البيانات).
2. أوامر الكتابة تطلب `--reason` عند الاستدعاء خارج حادث مفتوح.
3. لا أمر يحذف بيانات دائمًا؛ الحذف إجراء مراجَع خارج الواجهة.
