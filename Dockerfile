# 1. استخدام صورة بايثون خفيفة ومستقرة
FROM python:3.10-slim

# 2. تعيين مجلد العمل داخل الحاوية
WORKDIR /app

# 3. منع بايثون من كتابة ملفات pyc وضمان طباعة المخرجات (Logs) فوراً
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 4. تثبيت الحزم والمكتبات المطلوبة
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. نسخ باقي ملفات المشروع (main.py وأي ملفات ملحقة)
COPY . .

# 6. فتح المنفذ 8000 لواجهة الويب FastAPI / Tailwind
EXPOSE 8000

# 7. أمر تشغيل التطبيق
CMD ["python", "main.py"]