# دليل الإعداد الأول للمستودع (مرة واحدة، قبل أول وسم إصدار)

الهدف: إغلاق تحذيرات `tools/check_supply_chain.py` كلها. بوابة الإصدار لا تستطيع التحقق من تثبيت نفسها، فالخطوات 1–4 يدوية ومقصودة.

1. **تثبيت الإجراءات بمعرّف commit كامل.** لكل سطر `uses:` في `.github/workflows/validate.yml`، بما فيها مهمة `release-gate`:
   `git ls-remote https://github.com/actions/checkout refs/tags/v4` ثم استبدال `@v4` بالمعرّف ذي الأربعين حرفًا، مع إبقاء الوسم تعليقًا: `actions/checkout@<sha> # v4`. ويُتحقق من المعرّف في صفحة إصدارات الإجراء نفسه.
2. **بصمات التبعيات.** `pip install pip-tools && pip-compile --generate-hashes requirements-ci.txt -o requirements-ci.lock`، ثم تغيير CI إلى `pip install --require-hashes -r requirements-ci.lock`.
3. **CODEOWNERS.** استبدال `@OWNER_HANDLE` في `.github/CODEOWNERS` بحساب المالك الفعلي.
   **الواقع في مشروع بمؤسس واحد:** المراجع هو المؤسس نفسه، وGitHub لا يسمح لصاحب الطلب بالموافقة على طلبه. لذلك لا يُفعَّل «اشتراط مراجعة مالك الشيفرة» الآن، وإلا توقف العمل. CODEOWNERS يطلب المراجعة ولا يفرضها؛ يمنع الخطأ العارض في الحساب الخطأ، لا الإجراء المتعمد. الفرض الفعلي تقني: مهمة `workflow-guard` تفحص بصرامة أي طلب يغيّر `.github/`، ولا تُدمج الطلبات إلا بنجاح الفحوص. عند انضمام مشرف ثانٍ يُفعَّل اشتراط مراجعة مالك الشيفرة ويصبح هو المراجع.
4. **حماية الفرع الرئيسي.** اشتراط طلب دمج ونجاح الفحص الواحد `gate` (منذ 1.8؛ يفشل ما لم تنجح كل المهام الأخرى على commit نفسه، فلا تُقرأ مهمة متخطاة أو ملغاة نجاحًا)، وتطبيق القواعد على المسؤولين أيضًا، ومنع الدفع المباشر وإعادة كتابة التاريخ، وتفعيل فحص الأسرار في المستودع.
5. **أول تشغيل للعزل (قبل أي شيء آخر).** `bash db/tests/run_local.sh` على جهاز فيه Docker، ثم دفع أول فرع لتشغيل مهمة `database` في CI. لا وسم إصدار قبل نجاحها؛ مهمة `release-gate` تعتمد عليها.
6. **التحقق.** `python3 tools/check_supply_chain.py --strict` يجب أن يعطي صفر فشل وصفر تحذير. بعدها فقط يُنشأ أول وسم `v*`.
7. **لاحقًا.** أي تعديل على سطر `uses:` يمر بمراجعة المالك (CODEOWNERS)، ولا يُدمج إلا بمعرّف commit كامل؛ الفحص الصارم في بوابة الإصدار يمنع وسمًا بغير ذلك.


(1.8) مسارات البوابة المحمية بـ CODEOWNERS وبحارس البوابة: `.github/` و`tools/` و`policies/` و`evals/` و`db/migrations/` و`db/tests/` و`db/local/` والمصفوفة ومعجم الأخطاء وجرد definer وVERSION (البيان MANIFEST.json يتغير مع كل commit فيفحصه الحارس ولا يحرسه). حارس البوابة يأخذ برنامجه من commit الأساس، فلا يحتاج إعدادًا؛ لكنه لا يعمل على أول commit في المستودع (لا أساس)، فيُراجع ذلك الـ commit يدويًا.

## متغيرات التشغيل الحقيقي (hermes-app)
كل مجموعة تُضبط كاملة أو تُترك كلها. نصف مجموعة يمنع الخدمة من الإقلاع، بدل أن تعمل ناقصة.

| المجموعة | المتغيرات | من أين | المواصفة |
|---|---|---|---|
| الوضع | `HERMES_GRAPH=live` | — | 28.14 |
| واتساب | `HERMES_GRAPH_TOKEN` (رمز مستخدم نظام Meta) | Meta Business Manager | 28.14 |
| ماسنجر وإنستغرام والنشر | `HERMES_GRAPH_ACCOUNT_TOKENS` (JSON: معرّف الصفحة أو الحساب ← رمزه) | Meta | 28.21، 28.22 |
| دخول المالك | `HERMES_SUPABASE_URL`، `HERMES_SUPABASE_ANON_KEY`، `HERMES_JWT_SECRET` (السر القديم، أو 32 حرفًا عشوائيًا مع مفاتيح التوقيع) | مشروع Supabase | 28.12، 28.26 |
| البريد الوارد وأحداثه | `HERMES_EMAIL_INBOUND_SECRETS` (`user:password`)، ويُضبط الرابطان في Postmark: `https://user:password@<التطبيق>/email/inbound` و`/email/events` | Postmark | 28.25، 28.28 |
| ردود البريد | `HERMES_POSTMARK_TOKEN`، `HERMES_EMAIL_SENDERS` (JSON: العنوان الوارد ← `الاسم <المرسل الموثّق>`) | Postmark (توثيق النطاق SPF/DKIM) | 28.25 |
| تنبيهات المالك بالبريد | `HERMES_POSTMARK_TOKEN`، `HERMES_NOTIFY_SENDER`، `HERMES_PORTAL_URL` (`https://<التطبيق>/portal`) | Postmark | 28.27 |
| ربط تيك توك | `HERMES_TIKTOK_CLIENT_KEY`، `HERMES_TIKTOK_CLIENT_SECRET`، `HERMES_TIKTOK_REDIRECT_URI` (`https://<التطبيق>/portal/connect/tiktok/callback`)، `HERMES_TOKEN_KEYS` | TikTok for Developers (صلاحية `video.publish`) | 28.30 |
| خصوصية تيك توك | `HERMES_TIKTOK_PRIVACY` (الافتراضي `SELF_ONLY` حتى تراجع تيك توك التطبيق) | — | 28.23 |
| المراقبة | `HERMES_MONITOR_TOKEN`، وفي GitHub: `HERMES_DEPS_URL` و`HERMES_MONITOR_TOKEN` | — | 28.15 |

`HERMES_TOKEN_KEYS`: مفتاح أو أكثر، كل منها 32 بايتًا بترميز base64، مفصولة بفواصل والأحدث أولًا. يولَّد بـ `python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`، ولا يُحفظ إلا في متغيرات الخدمة.
