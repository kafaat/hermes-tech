# تعليمات العمل لأي وكيل أو مطوّر (AGENTS.md)

يقرؤه Codex وغيره من هذا الاسم، ويستورده `CLAUDE.md` لـ Claude Code؛ فهو مصدر واحد. حالة المشروع في المستودع لا في ذاكرة محادثة:
من يستأنف العمل يبدأ من هنا، لا من سؤال الجلسة السابقة.

## من أين تبدأ
1. `README.md` للشجرة والأوامر، و`Hermes_Technical_Specification_v1.8.md` للمواصفة. العمل بعد الإصدار مسجّل في القسم 28 بأقسام مرقّمة (28.1 …)، آخرها آخر ما بُني.
2. `git log --oneline -20`: رسالة كل التزام تذكر القسم والادعاء، مثل `(spec 28.41, P28)`.
3. `python tools/check_claims.py`: كل ادعاء «يجب» في `docs/claims.yaml` وما يفرضه، بحالة محسوبة لا مكتوبة.
4. ما لم يُبنَ بعد ولماذا: قسم «غير مبني» في `cli/hermes_cli_spec.md`، وخطوات المالك في `docs/setup_guide.md`.

«المنسّق» خدمة هذا المستودع وسلطة بيانات العملاء. «Hermes Agent» (Nous Research) مساعد داخلي للمؤسس فقط، لا يصل إلى أي عميل (ADR-0002).

## التتبع: من المتطلب إلى الإصدار
القسم في المواصفة ← الادعاء في `docs/claims.yaml` ← الترحيل (رأسه يذكر القسم) والكود ← الاختبار (مرجع الادعاء) ← الالتزام (رسالته تذكر القسم والادعاء) ← الإصدار المفعّل (`app.service_build`: commit وجيل).
أثر تعديل قبل تنفيذه: `git grep -n "28.41"` و`git log --grep "28.41"` وادعاءات القسم في `docs/claims.yaml`؛ تظهر الترحيلات والكود والاختبارات والالتزامات التي تمسّه.
الأرقام والجرود والأقسام المولّدة في المواصفة لا تُعدَّل يدويًا: `python tools/check_spec.py --write` ثم `python tools/build_manifest.py`.

## قبل كل رفع
```bash
python tools/validate.py && python tools/derive.py --check && python tools/run_evals.py
python tools/check_supply_chain.py --strict && python tools/check_claims.py
python tools/check_spec.py --check && python tools/build_manifest.py --check
python -m unittest discover -s tests          # ومرة في بيئة لا فيها إلا requirements-ci.txt، كما في CI (بلا psycopg)
pip-audit -r requirements-ci.txt --strict && pip-audit -r requirements-service.txt --strict
bash db/tests/run_local.sh                    # مهمة database كاملة: Docker، أو PGHOST=... لخادم قائم
base=$(git rev-parse origin/main)             # حارس البوابة كما يشغّله CI: برنامج الأساس على تغييرك
git show "$base:tools/gate_guard.py" > /tmp/gate_guard.py && python /tmp/gate_guard.py --base "$base"
git show "$base:tools/test_guard.py" > /tmp/test_guard.py && python /tmp/test_guard.py --base "$base"   # لا اختبار مُتخطى أو محذوف (28.43)
```
بعد الرفع تُقرأ نتيجة CI لكل التزام مرفوع، لا لآخرها وحده.
اختبار يُحذف أو يُعاد تسميته عن قصد يُسمّى في آخر رسالة الالتزام: `Tests-Removed: <معرّف الاختبار> — <السبب>`، والمعرّف كما يطبعه الحارس.

## ترتيب الرفع
وصف ترحيل جديد أو وحدة اختبار جديدة (`MIGRATION_DESCRIPTIONS` و`TEST_DESCRIPTIONS` في `tools/check_spec.py`)، أو استثناء في قواعد المتحقق، أو تبعية تستوردها الاختبارات: يُرفع إلى الفرع الرئيسي أولًا في التزام مستقل، ثم التغيير. التفصيل والسبب في `docs/delivery_protocol.md`، قسم «ترتيب الرفع حين يتغير ما يحكم به الحارس».

## قواعد لا استثناء فيها
- **المستودع عام.** لا عنوان staging أو إنتاج، ولا سر أو رمز، ولا رقم هاتف أو بريد حقيقي، في ملف أو رسالة التزام. العناوين والأسرار في أسرار GitHub ومتغيرات Railway وحدها.
- لا يُضعَف مدقق أو اختبار ليمرّ تغيير؛ مسارات البوابة يحكم عليها مدققو الأساس (`tools/gate_guard.py`).
- الترحيل المطبّق لا يُعدَّل؛ التغيير ترحيل جديد برقم تالٍ. لا منح حذف، ولا دالة definer خارج الجرد (`docs/security_definer_inventory.md`).
- بيانات العملاء الحقيقية لا تدخل الاختبارات ولا السجلات ولا المحادثات؛ بيانات القبول تمر بـ `tools/anonymize.py`.
- حذف خدمة أو قاعدة أو بيانات في staging أو الإنتاج، وكل إجراء لا يُتراجع عنه: بموافقة المالك الصريحة أولًا.

## البيئات
- **CI** (`.github/workflows/validate.yml`): مهمة `gate` هي الفحص المطلوب الوحيد؛ تعمل مع كل دفع وطلب دمج وأسبوعيًا.
- **staging** على Railway (`ops/railway/`): الدفع إلى الفرع الرئيسي ينشر hermes-app؛ خطوة ما قبل النشر (`migrate.sh`) تطبّق الترحيلات وتفعّل الإصدار بالجيل التالي حيث Graph محاكى فقط (28.38). خدمة db-migrate تشغّل العزل وواجهة الأوامر والمسار الكامل على staging.
- **الإنتاج** غير منشور بعد. تفعيل إصدار فيه قرار مشغّل (`app.activate_service_build`، 0031)، لا خطوة آلية.
