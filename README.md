# Hermes · الحزمة التقنية 1.8

حزمة تنفيذية مستقلة عن الوثيقة المالية: عقود الوكلاء، ومخطط قاعدة البيانات مع العزل بين العملاء وطبقة السلطة،
وسياسة تصعيد الشكاوى وسياسة القبول الإحصائية، وطبقة الفرض المرجعية، ومسارات الخدمة المطلوبة قبل التجربة،
ودليل الحوادث، والمخططات، وأدوات التحقق والاختبارات.
الشرح الكامل في `Hermes_Technical_Specification_v1.8.md` في جذر الحزمة. أرقام الحالة وجرودها فيه مولّدة
(`tools/check_spec.py`) ويفحصها CI، فلا تُعدَّل يدويًا. نسختا Word/PDF خارج الحزمة وتُولَّدان من هذا الملف.

## الشجرة (المختصرة؛ الكاملة مولّدة في §3 من المواصفة)
```
hermes-tech/
├── contracts/        عقود الوكلاء والفهرس (مصدر الحقيقة)
├── runtime/          مخططات حالة التشغيل وسجل النداءات وباب بيانات Hermes Agent
├── policies/         الشكاوى · حارس المحتوى · الجلب الآمن · سياسة القبول الإحصائية (مصدر واحد)
├── db/migrations/    0001 … 0010 (0009 طبقة السلطة · 0010 إغلاق مراجعة 1.7)
├── db/local/         محاكاة Supabase ومالك عادي hermes_owner للاختبار
├── db/tests/         حالات العزل والكتالوج + ثلاثة سكربتات سباق + run_isolation.sh · run_local.sh
├── service/          معالج webhook · الموزّع · الزاحف · القوالب · حجب السجلات · مرشح المراقبة · المهام الدورية
├── tools/            التحقق · الفرض المرجعي · القبول · المصفوفة · اتساق المواصفة · حارس البوابة · البيان
├── tests/            اختبارات unittest (الجرد في §15.2)
├── evals/            مجموعات التقييم (تطوير فقط) وسجل قبول النماذج
├── docs/             الجرود · المصفوفة claims.yaml · معجم الأخطاء · البروتوكولات · adr/ · ردود المراجعات
├── derived/ · ops/ · cli/ · diagrams/ · reports/
└── .github/          workflows/validate.yml (مهمة gate واحدة مطلوبة) · CODEOWNERS
```

## التشغيل
```bash
python tools/validate.py               # يجب: TOTAL n/n checks passed
python tools/derive.py --check         # derived/ محدَّث
python -m unittest discover -s tests   # كل الاختبارات
python tools/run_evals.py              # مجموعات التقييم (تطوير؛ لا حكم قبول)
python tools/check_supply_chain.py     # إجراءات غير مثبّتة وتبعيات بلا بصمات
python tools/check_claims.py           # كل ادعاء «يجب» مقابل ما يفرضه، بحالة محسوبة
python tools/check_spec.py --check     # كل رقم وجرد في المواصفة مطابق لمصدره
python tools/build_manifest.py --check # الملفات والتقارير تخص الإصدار المعلن في VERSION
bash db/tests/run_local.sh             # أول بوابة قبل التجربة: الترحيلات بمالك عادي، كل الحالات، ثلاثة سباقات (Docker)
```

### الخدمة والمسار الكامل (P1، القسم 28 من المواصفة)
```bash
pip install --require-hashes -r requirements-service.txt      # مشغّل Postgres بقفل ببصمات
DATABASE_URL=... python db/tests/e2e_pilot.py                 # يشغّل service.app ويقود المسار كاملًا عبر HTTP (26 فحصًا)
DATABASE_URL=... HERMES_WEBHOOK_SECRETS=... HERMES_GRAPH=simulate python -m service.app   # نقطة webhook + العامل
```
قبلها تُطبَّق الترحيلات (db/tests/run_isolation.sh أو ops/railway/migrate.sh). واجهة Graph محاكاة؛ الخدمة ترفض أي وضع آخر.
staging على Railway: الخدمة hermes-app، والمهمة db-migrate (الترحيلات ثم العزل ثم المسار الكامل) مع كل دفع إلى main.
`validate.py` يستخدم `jsonschema` إن وُجد، وإلا مدققًا مدمجًا يغطي كل الكلمات المستخدمة في المخططات.
الترحيلات لا تُطبَّق يدويًا بدور superuser: `db/tests/run_isolation.sh` يطبّقها بدور `hermes_owner` كما يجب في الإنتاج.

## قواعد الحزمة
1. أي نموذج يستخدمه عقد يجب أن يكون في `registry.approved_models` بسعره ومصدره.
2. الإجراء ذو الأثر الخارجي يُكتب في `proposals` لا في `allow` ولا في `deny`؛ يقرره إنسان، وتستهلك القاعدة موافقته مرة.
3. مجموع سقوف الوكلاء الشهرية ≤ سقف الذكاء الاصطناعي لكل عميل نشط (1.13$).
4. كل جدول في `app` عليه RLS؛ كل استثناء من FORCE في جرد منشور يطابقه المتحقق؛ لا صلاحية حذف لأدوار التطبيق.
5. `derived/` والأقسام المولّدة في المواصفة لا تُحرَّر يدويًا؛ CI يرفض أي فرق.
6. قناة واتساب رسمية فقط (Cloud API)؛ Hermes Agent أداة داخلية للمؤسس فقط (docs/adr).
7. لا مكوّن خارجي يدخل قبل تسجيل ترخيصه في docs/licensing_matrix.md.
8. أي جلب من الخادم عبر `service/crawler.py` وحارس `tools/safe_fetch.py` وحدهما؛ أي نص مالك في صفحة عبر `service/render.py` وحده.
