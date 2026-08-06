
## 1.2.2 — UTC invoice timer fix

- Payment countdown is now based on server-authoritative remaining seconds, not the phone clock.
- SQLite naive timestamps are normalized to UTC before API or template output.
- All invoice timestamps are returned with a trailing `Z`.
- Fresh invoices no longer appear expired on Iran/Italy time zones.

# BluePay v1.2.1 — Integration Core

بلوپی یک زیرساخت اتصال پرداخت برای سایت‌ها و ربات‌ها است. تمرکز این نسخه روی صدور فاکتور از API یا ربات، تأیید پیامک بانکی، Callback امضاشده، مدیریت فروشگاه و کارت مقصد، کیف پول و گزارش‌ها است.

## امکانات فعال

- ساخت فاکتور از API برای هر فروشگاه
- ساخت فاکتور دستی از داخل ربات
- لینک پرداخت و صفحه رسید
- تأیید خودکار از پیامک بانکی
- Callback امضاشده و Retry پایدار
- فروشگاه و API Key مستقل
- کارت‌های مقصد، کیف پول و سیاست کارمزد
- Sandbox، Timeline، گزارش مالی و پنل پذیرنده
- دستگاه امن SMS Forwarder
- تیم، Audit Log، سلامت سرویس و ابزارهای عملیاتی

## تغییر نسخه 1.2.1

- مرکز آپشن‌های تجاری از منوی ربات حذف شد.
- اتوماسیون‌ها و Workerهای فروشگاهی غیرفعال شدند.
- API و صفحات محصولات، لینک دائمی، اشتراک، تخفیف، شعبه و صندوق دیگر Mount نمی‌شوند.
- ساخت فاکتور دستی به‌عنوان جریان اصلی داخل ربات باقی ماند.
- جدول‌ها و Migrationهای نسخه 1.2.0 برای سازگاری دیتابیس حفظ شده‌اند و داده‌ای حذف نمی‌شود.
