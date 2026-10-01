#!/usr/bin/env python3
"""The specification's current-state numbers and inventories are GENERATED, never typed (v1.8).

  python tools/check_spec.py --check   exit 1 if a generated region is stale or missing, or the version is inconsistent
  python tools/check_spec.py --write   regenerate every region in the specification

The review of 1.7 found the text disagreeing with the package in eight places (six migrations in the tree,
eight in CI, 105 tests in a table that claimed 116, four pre-pilot items where the matrix had six, six
warnings where there were seven, 24 SQL cases where there were 37 ...). Each of those facts now lives
between <!-- gen:NAME --> and <!-- /gen:NAME --> markers and is recomputed from its source:
  status      validator, tests, evaluation, supply chain            -> header table
  tree        the files on disk                                     -> §3
  migrations  db/migrations/*.sql headers                           -> §4
  sql_cases   db/tests/rls_isolation_test.sql                       -> §5.5
  force       docs/security_definer_inventory.md FORCE-EXCEPTIONS   -> §5.3
  validator   tools/validate.py summary                             -> §15.1
  tests       unittest discovery (static)                           -> §15.2
  supply      tools/check_supply_chain.py                           -> §16
  conditions  docs/claims.yaml decisions fix_before_pilot + status  -> §21.3
  claims      tools/check_claims.py statuses                        -> §27
  errors      docs/error_codes.yaml                                 -> Appendix A
History (changelogs, sections about earlier versions) keeps its old numbers on purpose and is not generated.
"""
from __future__ import annotations
import re, subprocess, sys, unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
VERSION = (ROOT / "VERSION").read_text().strip()
SPEC = ROOT / f"Hermes_Technical_Specification_v{VERSION}.md"

TEST_DESCRIPTIONS = {
    "test_complaints": "التطبيع، حدود الكلمات، السوابق، الاستثناءات، التوجيه حسب القناة، اللهجة اليمنية",
    "test_enforcement": "كل رموز الرفض، المقترحات، سقف الوكيل مقابل سقف العميل، عدم التكرار، إعادة المحاولة، قاطع الدائرة",
    "test_validator_negative": "اختبارات طفرات: يُدخل كل عيب وُجد في المراجعات ويتأكد أن المتحقق يرفضه",
    "test_content_guard": "الأسعار بالأرقام العربية والهندية، الادعاءات الصحية، الوعود، الهواتف، الروابط المقنّعة، الإساءة",
    "test_triage": "المرحلة الثانية تضيف ولا تلغي، العتبة، استفسارات المالك",
    "test_ops_summary": "باب بيانات Hermes Agent: لا اسم ولا هاتف ولا نص مدسوس؛ الرمز المجهول يُحجر",
    "test_service_supabase_auth": "دخول المالك عبر Supabase Auth: رمز لمرة واحدة بالبريد، الجلسة من الخادم لا المتصفح، وتجديدها قبل انتهائها",
    "test_service_graph_live": "مرسل Graph الحقيقي: مغلق دون إعداد صريح، المضيف الوحيد graph.facebook.com عبر المسار المثبّت، والنتيجة الملتبسة لا تُعاد آليًا",
    "test_service_site_seo": "بيانات موقع العميل المنظمة: JSON-LD من الحقائق المعتمدة وحدها، لا يكسر وسم السكربت، وقارئنا يقرؤه كاملًا؛ وsitemap وrobots",
    "test_service_qr": "رمز QR لرابط واتساب يُولَّد محليًا: ترميز صحيح يطابق مرجعًا، ورابط wa.me ثابت لا تحويل عبر طرف ثالث",
    "test_service_media_policy": "صور المنشورات: صورة المالك كما هي، وصورة المخزون أو المولّدة لا تُنشر إلا بوسم «صورة توضيحية» ومصدر مرخّص",
    "test_deps_monitor": "المراقب الخارجي لـ/deps: يقرأ الأرقام بالرمز، ويفشل على الحالة لا على النص، ولا يطبع سرًا",
    "test_anonymize": "إخفاء الهواتف بصيغها والبريد والروابط والأرقام قبل التصنيف",
    "test_admission": "قبول المزوّد الثاني والسياسة الإحصائية الواحدة: المسار، حد المحاولات، بيانات التطوير، مثال المراجعة",
    "test_supply_chain": "غياب CODEOWNERS فشل، pull_request_target فشل، والتعليقات لا تُعدّ إجراءات",
    "test_regressions_v15": "عيوب 1.1: التزامن والتكرار والمفاتيح والمرحلة الثانية والمسبار وسقف المهمة",
    "test_concurrency_properties": "خصائص التزامن بخيوط كثيرة: لا تجاوز لسقف ولا تنفيذ مكرر",
    "test_regressions_v16": "مراجعة 1.5: عقود الإيجار، الإخفاق النهائي، الإيقاف، التجاوز المحتسب، الخطأ غير المعروف",
    "test_regressions_v17": "M1–M3: الميزانية بالشهر، الحجز بالحد المحسوب، الإيقاف عند خرق سقف النداء",
    "test_claims": "مصفوفة الادعاءات: مرجع مكسور، فجوة بلا قرار، حجب الإصدار، الحالة محسوبة لا مكتوبة",
    "test_derived": "الملفات المشتقة مطابقة لمصدرها",
    "test_safe_fetch": "حارس SSRF: البيانات الوصفية والشبكات الخاصة وIPv6 الحامل لـ IPv4 وإعادة التوجيه",
    "test_run_evals_exit": "حارس المحتوى يوقف CI دائمًا؛ الشكاوى تقرير إلا مع --gate standing-send",
    "test_service_worker": "حلقة العامل (P1): الشكوى وسؤال السعر يُصعَّدان، والرد من حقيقة اعتمدها المالك ويجتاز الحارس فقط",
    "test_service_webhook": "معالج webhook: HMAC على البايتات الخام قبل التحليل، 401 بلا تخزين، إعادة التسليم نجاح، القناة المخاطَبة",
    "test_service_dispatcher": "الموزّع: الغامض لا يُعاد، قبل الإرسال يُحرَّر، الرفض نهائي، مطابقة رموز الآلة للترحيل 0010",
    "test_service_redact": "حجب السجلات: الهواتف بالأرقام العربية، حقول المحتوى، الرموز، نص الاستثناءات",
    "test_service_crawler": "الزاحف: العنوان المثبّت، إعادة الحل لكل قفزة، الوكيل الوسيط من البيئة، robots وصفحات الدخول والحجم",
    "test_service_render": "القوالب: التهريب، روابط javascript: بكل تمويه، مواضع القالب الخطرة",
    "test_service_telemetry": "المراقبة: قائمة سماح للسمات، لا أحداث محتوى، لا بيانات شخصية في القيم المسموحة",
    "test_audit_checkpoint": "نقاط تحقق التدقيق الخارجية: إعادة بناء السلسلة، صفوف محذوفة، سطر مزوّر، رأس يتراجع",
    "test_service_boundaries": "مسار واحد لكل ضمان: الزاحف وحده يفتح اتصالًا، لا إدراج HTML خام، لا حلقة إعادة في الموزّع",
    "test_service_portal": "بوابة المالك: رمز الجلسة يُتحقق بصرامة، الإجراء مربوط بالجلسة (CSRF والأصل)، لا سكربت، كل قيمة مُهرَّبة",
    "test_service_structured": "حقائق المنافس المنظمة (JSON-LD): إعادة التصميم لا تغيّر شيئًا، تغيّر السعر يُكتشف ويُقال بالعربية، والمدخل العدائي محدود",
    "test_service_competitor": "فحص المنافسين اليومي: المستحقّون، والحالات الثلاث (حقائق، بلا بيانات منظمة، محجوب)، والفرق المحسوب",
    "test_gate_guard": "بوابة الإصدار: مدققات الأساس تحكم على إضعاف مدقق، ومهمة gate لا تقبل المتخطى والملغى",
    "test_spec_consistency": "المواصفة: كل قسم مولّد مطابق لمصدره، والإصدار متسق",
}
MIGRATION_DESCRIPTIONS = {
    "0001_core": "الأنواع والجداول والقيود",
    "0002_rls": "الأدوار ودوال الهوية وأمن الصف",
    "0003_guards_audit": "الحراسات التي لا يتجاوزها التطبيق وسلسلة التدقيق",
    "0004_measurement_views": "عروض القياس التي تغذّي بوابات القرار",
    "0005_v11_channels_quality_queue": "القنوات وwebhook والجودة والطابور والصندوق الصادر (1.1)",
    "0006_v12_hardening": "سحب EXECUTE العام، وقيد مصدر الأسماء، والاستعادة والتدوير (1.2)",
    "0007_v15_atomic_budget_idempotency": "الحجز الذري للميزانية وعدم التكرار والمسبار (1.5)",
    "0008_v16_leases_terminal_pause": "عقود الإيجار والإخفاق النهائي والإيقاف (1.6)",
    "0009_v17_authority": "طبقة السلطة: الاقتراح والقرار والاستهلاك وعميل العامل من عقد الإيجار (1.7)",
    "0010_v18_closure": "إغلاق مراجعة 1.7: ساعة الحائط، آلة حالات التسليم، توجيه webhook، تدقيق الآثار، المحو الدوري (1.8)",
    "0011_monitor_signals": "دور المراقبة: دالة واحدة تعيد أرقامًا لا صفوفًا لمراقب خارجي (تأخر المحو، صفوف تنتظر إنسانًا)",
    "0012_monitor_epoch": "موعد المحو المتوقع في دالة المراقبة: من آخر نجاح أو من بدء المراقبة، فلا إنذار كاذب قبل أول تشغيل",
    "0013_rls_customer_indexes": "فهارس customer_id للجداول التي تصفّي سياساتها به، وفحص كتالوج يمنع جدولًا جديدًا بلا فهرس",
    "0014_outbox_effect_once": "الأثر بلا موافقة يُدرج مرة واحدة لكل هدف: المهمة المعادة تستعيد صفها ولا ترسل تنبيهًا ثانيًا",
    "0015_outbox_resend_task": "«أعد الإرسال» من المشغّل يُدرج مهمته في المعاملة نفسها، فلا يبقى صف معلقًا بلا من يرسله",
    "0016_claim_task_kinds": "العامل لا يستلم إلا أنواع المهام التي يعرفها، فالنسخة القديمة أثناء النشر تترك الأنواع الجديدة",
    "0017_competitor_facts": "حقائق المنافس المنظمة وبصمة نص الصفحة في اللقطة، ودور مهمة الفحص اليومي بسقف شهري تفرضه القاعدة",
    "0019_inquiries_message_type": "نوع الرسالة الواردة (نص، صوت، صورة…) في الاستفسار، فيعرف المالك أن عليه سماع الرسالة في واتساب",
    "0020_standing_approvals": "الموافقة الدائمة: المالك يعتمد ردًا لموضوع منخفض الخطر مرة واحدة فيُرسل فورًا، بنصه المعتمد وحده، ويلغيه متى شاء",
    "0018_inquiries_event_once": "استفسار واحد لكل رسالة واردة: المهمة المعادة لا تدرج الرسالة مرة ثانية",
}
CLAIM_STATUS_AR = {"verified_here": "متحقق هنا", "ci_only": "في CI فقط", "structural": "بنيوي فقط",
                   "evidenced": "تشغيل مسجّل", "manual": "إجرائي",
                   "unbuilt": "غير مبني", "gap": "فجوة بقرار معلن"}


def run(*args):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True).stdout


def validator_counts():
    out = run("tools/validate.py")
    m = re.search(r"summary: schema (\d+) passed / (\d+) failed, semantic (\d+) passed / (\d+) failed, sql (\d+) passed / (\d+) failed", out)
    return tuple(int(x) for x in m.groups())


def test_inventory():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))
    counts = {}

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            elif type(t).__name__ in ("_FailedTest", "ModuleImportFailure"):
                raise SystemExit(f"test module failed to import: {t.id()}")
            else:
                counts[t.id().split(".")[0]] = counts.get(t.id().split(".")[0], 0) + 1
    walk(suite)
    return counts


def gen_tests():
    inv = test_inventory()
    missing = sorted(set(inv) - set(TEST_DESCRIPTIONS))
    if missing:
        raise SystemExit(f"describe these test modules in tools/check_spec.py TEST_DESCRIPTIONS: {missing}")
    rows = [f"| {m} | {TEST_DESCRIPTIONS[m]} | {n} |" for m, n in sorted(inv.items())]
    return "\n".join(["| المجموعة | ما تغطيه | العدد |", "| --- | --- | --- |", *rows,
                      f"| **المجموع** | يُحسب من اكتشاف الاختبارات لا يُكتب | **{sum(inv.values())}** |"])


def sql_cases():
    src = (ROOT / "db/tests/rls_isolation_test.sql").read_text(encoding="utf-8")
    cases = re.findall(r"^-- (\d+)\. (.+)$", src, re.M)
    return cases, src.count("raise notice 'PASS")


def gen_sql_cases():
    cases, notices = sql_cases()
    lines = [f"{n}. `{t}`" for n, t in cases]
    return "\n".join(lines + ["", f"الملف db/tests/rls_isolation_test.sql ينفّذ {len(cases)} حالة داخل معاملة تُلغى في النهاية، "
                                  f"ويطلب run_isolation.sh ظهور {notices} إشعار نجاح (بعض الحالات تطلق أكثر من إشعار)، "
                                  "بعد تطبيق كل الترحيلات بدور مالك عادي. العناوين بلغة الملف نفسه لأنها الحالات كما تُنفَّذ."])


def migrations():
    stems = [f.stem for f in sorted((ROOT / "db/migrations").glob("*.sql"))]
    missing = [m for m in stems if m not in MIGRATION_DESCRIPTIONS]
    if missing:
        raise SystemExit(f"describe these migrations in tools/check_spec.py MIGRATION_DESCRIPTIONS: {missing}")
    return [(m, MIGRATION_DESCRIPTIONS[m]) for m in stems]


def gen_migrations():
    ms = migrations()
    return "\n".join([f"الترحيلات {len(ms)} ملفات تُطبَّق بالترتيب ولا يُعدَّل أحدها بعد تطبيقه:", ""] +
                     [f"- `{n}` — {d}" for n, d in ms])


def gen_tree():
    def ls(d, pat="*"):
        return sorted(p.name for p in (ROOT / d).glob(pat) if p.is_file())
    mig = " · ".join(p.split("_", 1)[0] for p in ls("db/migrations", "*.sql"))
    return "\n".join([
        "```",
        "hermes-tech/",
        f"├── contracts/            agent_contract.schema.json · registry(.schema).json · {len(ls('contracts', '*.contract.json'))} × *.contract.json",
        "├── runtime/              agent_runtime_state · agent_call · ops_summary (schemas) · examples",
        f"├── policies/             {' · '.join(ls('policies'))}",
        f"├── db/migrations/        {mig}   ({len(ls('db/migrations', '*.sql'))} files)",
        "├── db/local/             0000_supabase_shim.sql   (plain Postgres testing only; creates the plain owner hermes_owner)",
        f"├── db/tests/             {' · '.join(ls('db/tests'))}",
        f"├── tools/                {' · '.join(ls('tools', '*.py'))}",
        f"├── service/              {' · '.join(ls('service', '*.py'))}",
        f"├── tests/                {len(ls('tests', 'test_*.py'))} modules (inventory in §15.2)",
        f"├── evals/                {' · '.join(ls('evals'))}",
        f"├── docs/                 {len(ls('docs'))} documents · adr/ ({len(ls('docs/adr'))} decisions)",
        "├── derived/              policy_matrix.md · agent_capabilities.json · alerts.yaml   (generated)",
        "├── ops/                  runbook · incident_template · slo.yaml · otel_genai_mapping.yaml · restore_drill.md",
        "│                         redteam/ · load/k6_webhook.js · hermes_agent/ (compose · squid · check_container · host_watch)",
        "├── cli/                  hermes_cli_spec.md",
        "├── diagrams/             *.dot sources + *.png",
        "├── reports/              validation · test · eval · supply_chain · claims · spec reports",
        "├── VERSION · MANIFEST.json · README.md",
        "└── .github/              workflows/validate.yml · CODEOWNERS",
        "```"])


def gen_force():
    import sql_state
    inv = (ROOT / "docs/security_definer_inventory.md").read_text(encoding="utf-8")
    tables = [t.strip() for t in re.search(r"^FORCE-EXCEPTIONS:\s*(.+)$", inv, re.M).group(1).split(",")]
    definers = sorted(k for k, v in sql_state.load().functions.items() if v["definer"])
    return (f"أمن الصف مُجبَر (FORCE) على كل جداول app عدا {len(tables)}: " + "، ".join(f"`{t}`" for t in tables) +
            ". سبب كل استثناء وما يعوّضه في docs/security_definer_inventory.md؛ المتحقق يطابق القائمة مع الحالة النهائية "
            "للترحيلات، والحالة 46 تطابقها مع الكتالوج الفعلي.\n\n"
            f"دوال SECURITY DEFINER بعد كل الترحيلات ({len(definers)}): " + "، ".join(f"`app.{d}()`" for d in definers) +
            ". لكل منها في الجرد ما تقرؤه وتكتبه ولماذا تحتاج صلاحيات المالك ومن ينفّذها والاختبار الذي يغطيها.")


def gen_validator():
    s, sf, se, sef, q, qf = validator_counts()
    return "\n".join(["| الطبقة | ما تفحصه | العدد |", "| --- | --- | --- |",
                      f"| المخططات | العقود والفهرس وحالة التشغيل وسجل النداءات والسياسات | {s} |",
                      f"| المنطق | قواعد 6.2، وسياسة الشكاوى، وسياسة القبول الواحدة، ومعجم الإخفاقات | {se} |",
                      f"| SQL | الحالة النهائية بعد كل الترحيلات: أمن الصف وFORCE والسياسات والمنح والدوال، والحراسات | {q} |",
                      f"| **المجموع** | فاشل: {sf + sef + qf} | **{s + se + q}** |"])


def supply():
    out = run("tools/check_supply_chain.py")
    return [l[5:] for l in out.splitlines() if l.startswith("WARN ")], [l[5:] for l in out.splitlines() if l.startswith("FAIL ")]


def gen_supply():
    warn, fail = supply()
    return (f"| تثبيت إجراءات CI وبصمات التبعيات وCODEOWNERS | {len(warn)} تحذيرات و{len(fail)} فشل: " + "؛ ".join(f"`{w}`" for w in warn) +
            " | docs/setup_guide.md بشبكة وحساب المالك؛ بوابة الإصدار تحوّلها إلى فشل، فلا وسم قبل الصفر |")


def claims():
    import check_claims
    data = yaml.safe_load((ROOT / "docs/claims.yaml").read_text(encoding="utf-8"))
    out = []
    for c in data["claims"]:
        st = check_claims.status(c, [])
        out.append((c, st, c.get("_ci_pending", False)))
    return out


def gen_conditions():
    rows = claims()
    ci = [c["id"] for c, st, pend in rows if pend]
    lines = [f"قائمة مولّدة من docs/claims.yaml (كل ادعاء قراره fix_before_pilot) وحالته المحسوبة الآن. «في CI فقط» تعني أن الاختبار مكتوب "
             f"ولم يُشغَّل؛ الشرط الأول مشترك بينها كلها: أول تشغيل ناجح لمهمة database على Postgres فعلي يغطي {len(ci)} ادعاءً "
             "بجزء إنتاجي ينتظر CI، ومنها كل حالات السلطة والعزل.", "",
             "| الادعاء | الشرط | الحالة الآن |", "| --- | --- | --- |"]
    for c, st, pend in rows:
        if str(c.get("decision", "")).startswith("fix_before_pilot"):
            lines.append(f"| {c['id']} | {c['claim']} | {CLAIM_STATUS_AR[st]}{' + ينتظر CI' if pend and st == 'verified_here' else ''} |")
    lines += ["", "خارج المصفوفة ولا يُولَّد: اختبار القبول للشكاوى ليس شرطًا للتجربة بل لأي سياسة إرسال دون موافقة لكل رد (22.2)."]
    return "\n".join(lines)


def gen_claims():
    rows = claims()
    counts = {}
    for _, st, _ in rows:
        counts[st] = counts.get(st, 0) + 1
    pend = sum(1 for _, _, p in rows if p)
    lines = ["| الحالة | العدد |", "| --- | --- |"]
    for k in ("verified_here", "ci_only", "structural", "evidenced", "manual", "unbuilt", "gap"):
        if counts.get(k):
            lines.append(f"| {CLAIM_STATUS_AR[k]} | {counts[k]} |")
    lines.append(f"| **المجموع** | **{len(rows)}** |")
    accepted = [c["id"] for c, _, _ in rows if str(c.get("decision", "")).startswith("accepted")]
    lines += ["", f"منها {pend} بجزء إنتاجي ينتظر أول تشغيل في CI. ادعاءات بقرار «مقبول» معلن لا تحجب الإصدار: "
                  + "، ".join(accepted) + "؛ منها A35 حد ثقة مسمّى (27.5) لا نقص تنفيذ."]
    return "\n".join(lines)


def gen_errors():
    lex = yaml.safe_load((ROOT / "docs/error_codes.yaml").read_text(encoding="utf-8"))["codes"]
    kind = {"error": "", "state": " (حالة لا خطأ)", "alarm": " (إنذار)"}
    rows = [f"| {k}{kind[v['kind']]} | {v['src']} | {v['since']} | {v['ar']} |" for k, v in lex.items()]
    return "\n".join([f"مولّد من docs/error_codes.yaml ({len(lex)} رمزًا)، المصدر نفسه لقاموس باب البيانات.", "",
                      "| الرمز | المصدر | منذ | المعنى |", "| --- | --- | --- | --- |", *rows])


def gen_status():
    s, sf, se, sef, q, qf = validator_counts()
    tests = sum(test_inventory().values())
    ev = run("tools/run_evals.py")
    golden = re.search(r"content guard golden: (\d+)/(\d+)", ev)
    recall = re.search(r"recall (\d+)/(\d+) = (\d+)%", ev)
    warn, fail = supply()
    cases, notices = sql_cases()
    return "\n".join([
        "| ما فُحص | النتيجة | الطريقة |", "| --- | --- | --- |",
        f"| مخططات JSON والعقود والفهرس والسياسات | {s} فحصًا ناجحًا من {s + sf} | tools/validate.py · الطبقة 1 |",
        f"| قواعد الاتساق المنطقي والسياسة الإحصائية ومعجم الإخفاقات | {se} فحصًا ناجحًا من {se + sef} | tools/validate.py · الطبقة 2 |",
        f"| الحالة النهائية لقاعدة البيانات بعد كل الترحيلات (فحص ثابت) | {q} فحصًا ناجحًا من {q + qf} | tools/validate.py · الطبقة 3 · tools/sql_state.py |",
        f"| الاختبارات الآلية | {tests} اختبارًا | python -m unittest discover -s tests (الجرد في 15.2) |",
        f"| حارس المحتوى على المجموعة الذهبية | {golden.group(1)}/{golden.group(2)} حكمًا مطابقًا | tools/run_evals.py |",
        f"| رصد الشكاوى بالكلمات وحدها (مجموعة تطوير {recall.group(2)} شكوى) | استدعاء {recall.group(1)}/{recall.group(2)} = {recall.group(3)}% | tools/run_evals.py · تقرير لا حكم قبول |",
        f"| سلسلة التوريد | {len(fail)} fail · {len(warn)} warn | tools/check_supply_chain.py (15.5 و16) |",
        f"| حالات SQL على Postgres فعلي | {len(cases)} حالة و{notices} إشعار نجاح مكتوبة؛ **لم يُنفَّذ أي منها هنا** | CI: run_isolation.sh وثلاثة سكربتات سباق |",
        "", f"المجموع: {s + se + q}/{s + sf + se + sef + q + qf} فحصًا ناجحًا في أداة التحقق، و{tests} اختبارًا. "
            "هذا الجدول مولّد (tools/check_spec.py)؛ لا يُعدَّل يدويًا."])


REGIONS = {"status": gen_status, "tree": gen_tree, "migrations": gen_migrations, "force": gen_force, "sql_cases": gen_sql_cases,
           "validator": gen_validator, "tests": gen_tests, "supply": gen_supply, "conditions": gen_conditions,
           "claims": gen_claims, "errors": gen_errors}


def region_re(name):
    return re.compile(rf"(<!-- gen:{name} -->\n)(.*?)(\n<!-- /gen:{name} -->)", re.S)


def main():
    write = "--write" in sys.argv
    problems = []
    if not SPEC.exists():
        print(f"SPEC MISSING: {SPEC.name}")
        return 1
    text = SPEC.read_text(encoding="utf-8")
    if f"الإصدار {VERSION} ·" not in text.split("\n", 8)[4]:
        problems.append(f"title line does not carry version {VERSION}")
    others = [p.name for p in ROOT.glob("Hermes_Technical_Specification_v*.md") if p != SPEC]
    if others:
        problems.append(f"specification of another version in the package: {others}")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if SPEC.name not in readme or f"الحزمة التقنية {VERSION}" not in readme:
        problems.append("README does not name this version and its specification")
    for name, fn in REGIONS.items():
        rx = region_re(name)
        m = rx.search(text)
        if not m:
            problems.append(f"region {name}: markers missing")
            continue
        want = fn()
        if m.group(2) != want:
            if write:
                text = text[:m.start(2)] + want + text[m.end(2):]
            else:
                problems.append(f"region {name}: stale (python tools/check_spec.py --write)")
    if write:
        SPEC.write_text(text, encoding="utf-8")
        problems = [p for p in problems if not p.startswith("region") or "markers" in p]
    for p in problems:
        print("FAIL", p)
    print(f"specification {SPEC.name}: {len(REGIONS)} generated regions, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
