# مواصفة واجهة الأوامر `hermes` · الإصدار 1.1

الواجهة أداة المؤسس والفريق التقني، مبنية في `cli/hermes.py` (المواصفة 28.35). كل أمر:
- يتطلب جلسة مشغّل (`app.operators`) بعامل ثانٍ (aal2)؛
- يُرسل إلى واجهة المشغّل في hermes-app (`POST /ops/api/<command>`) برمز جلسة المشغّل نفسه، ولا يحمل جهاز المشغّل اعتماد قاعدة البيانات؛
- يُنفَّذ في جلسة المشغّل في القاعدة، فتقرر `app.is_operator()` وسياسات المشغّل؛
- يكتب، إن كان كتابة، حدثًا في `app.audit_log` باسم الحدث المذكور، في معاملة التغيير نفسها؛
- يعيد رمز خروج: `0` نجاح · `2` مدخلات غير صالحة · `3` مرفوض بالصلاحية · `4` غير موجود · `5` تعارض حالة · `10` خطأ خادم.

```
python -m cli.hermes login --url https://<hermes-app> --email you@example.com   # رمز بالبريد ثم رمز تطبيق التحقق
python -m cli.hermes login --url https://<hermes-app> --staging                 # staging: HERMES_STAGING_LOGIN_CODE
python -m cli.hermes [--json] <الأمر> [الوسائط]
python -m cli.hermes logout
```

الأدوار: `read` لكل مشغّل؛ `ops` للمؤسس والتقني والتشغيل؛ `tech` للمؤسس والتقني؛ `founder` للمؤسس؛ `finance` للمؤسس والمالية (المال خط منفصل لا رتبة).
القائمة التي يتحقق منها الخادم هي `COMMANDS` في `service/ops_api.py`، والواجهة تُبنى منها، فلا تختلفان.

## التهيئة: من منشأة جديدة إلى اشتراك نشط

| الأمر | الوسائط | الأثر | الدور | حدث التدقيق |
|---|---|---|---|---|
| `customers list` / `show` | `[--status S]` / `--customer CUST` | المنشآت؛ وحالة تهيئة منشأة واحدة | read | — |
| `customers add` | `--name --sector --city --zone [--ref]` | منشأة جديدة بحالة lead | ops | `customer.created` |
| `owners link` / `unlink` | `--customer --user-id [--role owner\|staff]` | ربط مستخدم Supabase بالمنشأة (يصل البوابة) | ops | `owner.linked` / `owner.unlinked` |
| `channels list` | `[--customer]` | قنوات العميل الرسمية وحالتها | read | — |
| `channels add` | `--customer --kind [--external-id] [--name]` | قناة بانتظار التحقق؛ مفتاح نموذج الموقع يُولَّد | ops | `channel.added` |
| `channels verify` / `suspend` | `--id CHANNEL` | تفعيل بعد تحقق Meta، أو إيقاف | ops | `channel.verified` / `channel.suspended` |
| `facts list` / `add` | `--customer [--topic --fact --valid-until]` | حقيقة بانتظار موافقة المالك في البوابة | read / ops | `fact.added` |
| `competitors list` / `add` / `remove` | `--customer [--url --label]` / `--id` | منافسان نشطان حدًا (0003) | read / ops | `competitor.added` / `competitor.removed` |
| `site build` | `--customer --base-url https://<domain> --to DIR` | ملفات الموقع من المعلومات المعتمدة، في مجلد جديد، ببصمتها؛ يرفعها المشغّل كما هي (28.39) | ops | — |
| `billing list` | `--customer` | الاشتراكات والفواتير والدفعات | read | — |
| `billing subscribe` | `--customer --price-usd --start --end [--plan]` | اشتراك معلّق؛ المنشأة بانتظار الدفع | finance | `subscription.created` |
| `billing invoice` | `--subscription --amount --currency [--fx-rate-to-usd]` | فاتورة صادرة برقمها السنوي | finance | `invoice.issued` |
| `billing payment` | `--invoice --channel --receipt --amount --currency --received-at [--fee]` | دفعة بانتظار التحقق | finance | `payment.recorded` |
| `billing match` / `reject` | `--payment` | التحقق البشري من الإيصال؛ الفاتورة مدفوعة متى بلغت المطابقات مبلغها | finance | `payment.matched` / `payment.rejected` |
| `billing activate` | `--subscription` | الاشتراك والمنشأة نشطان؛ القاعدة ترفضه دون دفعة مطابقة | finance | `subscription.activated` |

## التشغيل

| الأمر | الوسائط | الأثر | الدور | حدث التدقيق |
|---|---|---|---|---|
| `whoami` | — | هوية الجلسة ودورها | read | — |
| `agent status` | `[--agent ID] [--customer CUST]` | حالة الدائرة والإنفاق والنداءات لهذا الشهر، والإيقافات | read | — |
| `agent health` | — | ملخص كل وكيل | read | — |
| `agent disable` | `--agent ID` | إيقاف وكيل عمومًا (`app.agent_pauses`) | ops | `agent.disabled` |
| `agent enable` | `--agent ID --reason` | رفع الإيقاف؛ السبب إلزامي ولو في حادث | ops | `agent.enabled` |
| `agent reset-circuit` | `--agent ID --customer CUST` | إغلاق الدائرة يدويًا | tech | `agent.circuit_reset` |
| `calls` | `[--agent] [--customer] [--since 24h] [--outcome X\|!X] [--limit N]` | قراءة سجل النداءات | read | — |
| `cost` | `[--customer] [--since 30d] [--group-by customer\|agent]` | تقارير التكلفة | read | — |
| `queue breakdown` | — | عمق الطابور حسب الوكيل والنوع والأولوية | read | — |
| `approvals list` | `[--customer] [--pending]` | عرض المقترحات | read | — |
| `outbox attention` | — | صفوف الصندوق الصادر التي تنتظر إنسانًا (v_outbox_attention) | read | — |
| `outbox resolve` | `--id OUTBOX --resolution confirmed_sent\|resend\|abandon --reason TEXT` | حسم إرسال غامض (app.resolve_outbox)؛ لا حسم دون فحص سجل المزوّد أولًا | ops | تكتبه الدالة نفسها |
| `flags list` / `resolve` | `[--customer] [--open]` / `--id FLAG --status confirmed\|dismissed` | أعلام content_guard وagent_quality | read / ops | `flag.resolved` |
| `freeze` / `unfreeze` | `--customer CUST` | إيقاف المنشأة وكل قنواتها النشطة فلا يُوجَّه إليها جديد؛ الرفع يعيد الحالة السابقة والقنوات التي أوقفها التجميد وحدها | ops | `customer.frozen` / `customer.unfrozen` |
| `incident list` / `open` / `update` / `close` | `[--open]` / `--severity --category [--customers]` / `--id --field --value` / `--id` | إدارة الحادث في `app.incidents`؛ لا إغلاق دون سبب جذري ووقاية | read / ops | `incident.*` |
| `gates record` | `--gate ID --value X --source S [--note]` | تسجيل قياس بوابة في `app.gate_measurements` | founder | `gate.recorded` |
| `logs export` | `--customer CUST [--since 72h] --to PATH` | الأدلة إلى ملف جديد (0600) لا يُكتب فوقه | founder | `logs.exported` |

قواعد:
1. أوامر الكتابة تطلب `--reason` (5 أحرف أو أكثر) عند الاستدعاء خارج حادث مفتوح؛ والسبب يُكتب في صف التدقيق.
2. لا أمر يحذف بيانات دائمًا؛ الحذف إجراء مراجَع خارج الواجهة. «الإزالة» تعطيل (`active = false`).
3. الوسيط المجهول يُرفض ولا يُتجاهل؛ كل وسيط مقيد بنوعه وطوله.
4. التجميد لا يوقف أثرًا وافق عليه المالك قبله؛ يظهر في `outbox attention` ويُحسم هناك.

## غير مبني، ولماذا

| الأمر | السبب |
|---|---|
| `approvals execute` | الصندوق الصادر ينفّذ الأثر الموافق عليه ويستهلك موافقته مرة (0009، 0010)؛ مسار تنفيذ يدوي ثانٍ يفتح بابًا للتكرار |
| `campaign pause` / `resume` | لا حملات جماعية مبنية |
| `worker add` / `remove` | عدد النسخ إعداد في Railway، لا في القاعدة |
| `deploy` / `rollback` | النشر الآلي غير مبني: حد Cloudflare Pages 100 مشروع لكل حساب، وCloudflare توصي بـ Workers للجديد؛ يُقرَّر المسار أولًا (28.39). الآن: `site build` ثم رفع يدوي |
| `secrets revoke` / `rotate` | مخزن الأسرار غير مربوط؛ الرموز المختومة (0028) تدور بـ`HERMES_TOKEN_KEYS` |
| `sessions terminate` | لا نقطة إدارية في Supabase لإنهاء جلسات مستخدم برقمه (مصدر supabase/auth): الخيار حذف صفوفه في `auth.sessions` أو حظره؛ والرمز الصادر يبقى صالحًا حتى انتهائه. مؤجل حتى ربط مشروع Supabase فعلي |
| `evals run` | `tools/run_evals.py` يعمل في CI؛ تسجيله في `app.eval_runs` لم يُربط |
