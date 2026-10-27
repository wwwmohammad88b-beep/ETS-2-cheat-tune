# TR-EURO-EDITOR — وضعیت طراحی رابط

## انجام‌شده

- ZIP سورس v2.6 بررسی شد؛ پروژه بازنویسی نشده و Tkinter قدیمی برای حالت جایگزین حفظ شده است.
- پوستهٔ جدید HTML/CSS آفلاین ساخته شده: نوار کناری، نوار بالا، کارت‌های گرد، پنل‌های تیرهٔ سرمه‌ای/فیروزه‌ای، وضعیت پایین پنجره و چینش RTL.
- صفحه‌های Dashboard، Tuning، Profiles & Saves، Garage، Game Settings، Mod Manager، Extras و Appearance/Theme Previews به رابط اضافه شده‌اند.
- گیج SVG دور موتور و سرعت با حرکت عقربه و مقدارهای تخمینی ایجاد شده است. صفحهٔ تیونینگ اعداد را به‌عنوان ورودی و تخمین نشان می‌دهد، نه تله‌متری زنده.
- هشت تم از پالت مرکزی Python تولید می‌شوند؛ رنگ‌ها مطابق مرجع تنظیم شده‌اند و تغییر تم، روز/شب، رنگ دلخواه و ذخیره‌سازی در ui.json متصل‌اند.
- اطلاعات ناوگان از extractor فعلی می‌آیند؛ فیلدهای ناموجود به‌صورت «اطلاعات در دسترس نیست» نشان داده می‌شوند. مسیر فایل‌ها در DOM با textContent درج می‌شود.
- ets2_web_bridge.py فقط APIهای محدود و نام‌دار دارد؛ ورودی‌ها اعتبارسنجی می‌شوند و مسیرهای سیو/پروفایل زیر پوشهٔ دادهٔ ETS2 دوباره بررسی می‌شوند.
- اجرای عادی WebView2 را امتحان می‌کند؛ با --classic رابط قبلی اجرا می‌شود و در صورت شکست WebView2 پیام داده می‌شود و برنامه به Tkinter برمی‌گردد.
- PyInstaller برای --onefile --windowed، آیکون، Crypto، WebView2 و دارایی‌های HTML/CSS/JS و قالب‌های ماد تنظیم شده است. Inno Setup مسیر نصب per-user، Start Menu، میانبر دسکتاپ اختیاری، Uninstaller و پیام تشخیص WebView2 دارد.
- Workflow، requirements و راهنمای README به‌روزرسانی شدند.

## حفظ کد هسته

فایل ets2_save_editor.py با TR-EURO-EDITOR-source-v2.6.zip مقایسه شد. AST این ۱۸ تابع/کلاس با نسخهٔ ورودی یکسان است:

make_backup, unique_backup_dir, backup_history, latest_backup, restore_save_backup, backup_root, edit_save, save_writes_supported, SaveFile, TextSave, BsiiSave, ProfileSii, write_profile_mods, validate_mod, read_config, write_config, create_tuning_mod, extract_fleet.

سیاست بکاپ چرخشی و منطق ویرایش سیو تغییر نکرده‌اند. تغییرات ترجمه و مقادیر accent در فایل اصلی جدا از این توابع‌اند.

## تست‌های اجراشده

- node --check ui/app.js — موفق.
- python -m py_compile ets2_save_editor.py ets2_web_bridge.py tests\test_web_ui.py — موفق.
- python -m unittest discover -s tests -v — ۵۶ تست، موفق (OK). هر ۴۹ تست موجود و ۷ تست جدید اجرا شدند. تست‌های Tk روی دسکتاپ ویندوز اجرا شدند؛ Tk پس از یکی از تست‌ها پیام teardown چاپ کرد اما فرایند با کد ۰ و نتیجهٔ OK تمام شد.
- فرمان ets2_save_editor.py --cli sample.sii جداگانه اجرا شد و مقادیر نمونه را چاپ کرد.
- AST compare بالا — موفق.

## هنوز تأیید نشده

- اجرای واقعی UI داخل pywebview/WebView2 انجام نشد؛ این محیط pywebview و pythonnet نصب‌شده ندارد.
- ساخت EXE و نصب‌کننده انجام نشد؛ PyInstaller، PyCryptodome و Inno Setup در runtime فعلی موجود نیستند.
- Workflow GitHub Actions واقعاً اجرا نشده است.
- تلاش برای گرفتن تصویر با Edge headless در این محیط ناموفق بود؛ Edge با خطای process -1073741819 خارج شد و screenshot ایجاد نشد. بنابراین بازبینی بصری تصویری و ماتریس screenshot کامل نیست.
- UI با سیو واقعی ETS2 در نسخهٔ 1.60.1.7 آزمایش نشد؛ خروجی سازگاری یا عملکرد داخل بازی ادعا نمی‌شود.
- شکل‌دهی فارسی و اجرای نصب‌کننده/WebView2 روی رایانهٔ مقصد تأیید نشده است.

## ادامهٔ کار به‌ترتیب

1. سورس ZIP را در ریپوی GitHub قرار بده و workflow Build Windows installer را اجرا کن.
2. اگر build قرمز شد، لاگ مرحلهٔ PyInstaller/Inno Setup را بررسی و خطا را اصلاح کن؛ موفقیت را از وجود artifact نتیجه بگیر، نه فقط سبزشدن تست‌ها.
3. ETS2SaveEditor.exe را روی Windows دارای WebView2 اجرا کن؛ صفحه‌ها، EN/FA، انتخاب پروفایل/سیو، تغییر تم زنده و لاگ خطا را بررسی کن.
4. ETS2SaveEditor_Setup.exe را نصب کن؛ Start Menu، میانبر اختیاری، اجرای per-user و Uninstall را بررسی کن.
5. بعد از دسترسی به یک سیو آزمایشی، Dashboard/Garage و عملیات فقط‌خواندنی را تطبیق بده؛ سپس با بکاپ جداگانه عملیات نوشتن را آزمایش کن.
6. screenshotهای Dashboard، Tuning، Garage و Appearance را در تم‌های Midnight Cyan، Racing Red و Light Professional و زبان‌های English/Persian بازبینی کن؛ اگر چینش یا رندر فارسی مشکل داشت اصلاح جداگانه انجام بده.

## Update: debug pass (music/radio, dashboard, tuning fix)
- Done: tuning truck-selection bug (UI sent ids, core uses display names), music/radio module + page + dashboard cards, reversible music fixes, honest runtime-feature cards, 25 new unit tests + jsdom end-to-end harness.
- NOT VERIFIED (no Windows/WebView2/game here): real audio output through WebView2, how the layout looks (no browser screenshots were possible), whether the in-game player accepts files after a fix, `g_set_time` argument syntax, Persian rendering on Windows.
- Still unsupported on purpose: infinite fuel, no damage, simulation speed (need a verified memory signature or a documented command for the exact game build).
- Run: `python -m unittest discover -s tests -v` (GUI tests need a display/Xvfb); e2e: `npm install jsdom` then `node tests/e2e/run.js .`.
