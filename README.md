# درگاه واسط پرداخت مستقیم — نسخه 0.2.1

این نسخه برای نصب ساده روی Railway طراحی شده و کاربر فقط دو متغیر وارد می‌کند:

```env
BOT_TOKEN=توکن BotFather
GITHUB_TOKEN=توکن Fine-grained گیت‌هاب با Contents: Read and write
```

موارد زیر خودکار انجام می‌شوند:

- تشخیص مالک، نام Repository و Branch از متغیرهای داخلی Railway
- ساخت SQLite و تمام جدول‌ها در اولین اجرا
- تولید Secretهای داخلی و کلید رمزنگاری کارت‌ها
- ساخت شاخه جداگانه `gateway-data`
- ذخیره نسخه رمزنگاری‌شده دیتابیس پس از تغییرات
- بازیابی دیتابیس در Deploy یا Restart بعدی
- انتشار ZIPهای بعدی روی همان Repository

بنابراین `DATABASE_URL`، Volume، نام Repository، Branch، Secretهای داخلی و تنظیم جداگانه دیتابیس لازم نیست.

> دیتابیس داخل شاخه `gateway-data` به‌صورت رمزنگاری‌شده ذخیره می‌شود و فایل خام دیتابیس در GitHub قرار نمی‌گیرد. برای بازیابی باید همان `BOT_TOKEN` حفظ شود. تعویض BOT_TOKEN بدون انتقال کلید می‌تواند بازیابی نسخه قبلی را غیرممکن کند.

## قابلیت‌ها

- ربات تلگرام پذیرندگان و مدیر
- ساخت فاکتور دستی و API
- کیف پول و رزرو کارمزد
- تقسیم کارمزد بین مشتری و پذیرنده
- ثبت چند کارت بانکی
- صفحه پرداخت و رسید
- دریافت و پردازش پیامک چندبانکی
- بررسی دستی پیامک‌های مبهم
- Callback امضاشده
- آپدیت کامل پروژه با ZIP از داخل ربات
- دیتابیس خودکار بدون PostgreSQL و Volume دستی

## نصب اولیه

1. یک Repository در GitHub بسازید و فقط فایل نصب اولیه را با نام `Dockerfile` داخل آن قرار دهید.
2. در Railway گزینه `Deploy from GitHub Repo` را انتخاب کنید.
3. فقط `BOT_TOKEN` و `GITHUB_TOKEN` را ثبت کنید.
4. `/start` را در ربات بزنید.
5. فایل ZIP کامل نسخه 0.2.1 را برای ربات ارسال کنید.
6. ربات فایل‌ها را Commit می‌کند و Railway نسخه کامل را خودکار Deploy می‌کند.

نام Repository و Branch از Railway دریافت می‌شود و دستور `/repo` یا `/github` برای تنظیم لازم نیست.

## دیتابیس خودکار

SQLite در مسیر زیر ساخته می‌شود:

```text
/app/runtime/gateway.db
```

بعد از هر Commit دیتابیس، یک Snapshot سازگار تهیه، رمزنگاری و در شاخه زیر ثبت می‌شود:

```text
gateway-data
```

هنگام Cold Start، اگر فایل محلی وجود نداشته باشد، Snapshot رمزنگاری‌شده دریافت و قبل از بازشدن SQLAlchemy بازیابی می‌شود.

وضعیت Backup از مسیر زیر قابل مشاهده است:

```http
GET /health
```

## API

ساخت فاکتور:

```http
POST /api/v1/invoices
X-API-Key: gw_xxx
Content-Type: application/json
```

```json
{
  "amount_toman": 200000,
  "order_id": "ORDER-1001",
  "description": "اشتراک یک ماهه",
  "fee_mode": "split",
  "ttl_minutes": 30
}
```

استعلام:

```http
GET /api/v1/invoices/{payment_id}
X-API-Key: gw_xxx
```

## Webhook پیامک

آدرس و Secret از داخل ربات نمایش داده می‌شود:

```http
POST /webhooks/sms
X-SMS-Secret: SECRET
Content-Type: application/json
```

```json
{
  "sender": "Bank Mellat",
  "message": "واریز به کارت ****1234 مبلغ 2,010,000 ریال شماره پیگیری 998877",
  "device_id": "phone-1"
}
```

## آپدیت بعدی

مدیر فقط ZIP جدید را برای ربات ارسال می‌کند. ربات Repository و Branch را خودکار تشخیص می‌دهد، Commit می‌سازد و Railway Autodeploy نسخه جدید را اجرا می‌کند.

هر ZIP باید دست‌کم شامل این فایل‌ها باشد:

```text
Dockerfile
requirements.txt
release.json
app/main.py
```

فایل‌های `.env` و کلیدهای حساس داخل ZIP پذیرفته نمی‌شوند.

## دستورات مدیر

```text
/credit TELEGRAM_ID AMOUNT_TOMAN
/setfee TELEGRAM_ID AMOUNT_TOMAN
/reviews
/approve SMS_ID PAYMENT_ID
/rejectsms SMS_ID
/github
```

دستور `/github` فقط مشخصات خودکار تشخیص‌داده‌شده را نمایش می‌دهد و چیزی تنظیم نمی‌کند.

## نکات مهم

- برای پردازش پول واقعی، Parser هر بانک را با نمونه پیامک واقعی همان بانک آزمایش کنید.
- در صورت وجود چند فاکتور هم‌مبلغ برای یک کارت، سیستم تأیید را حدس نمی‌زند و پیامک را برای بررسی دستی نگه می‌دارد.
- این روش ذخیره‌سازی برای نصب ساده و حجم کم طراحی شده است. برای ترافیک بالا و چند Replica، PostgreSQL مدیریت‌شده مناسب‌تر است.
- برای صفحه پرداخت و Webhook عمومی، سرویس نهایی باید یک دامنه عمومی Railway داشته باشد؛ در نصب با Railway Template این مورد خودکار است و در Deploy عادی ممکن است یک‌بار دکمه Generate Domain لازم باشد.
