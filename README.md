# Star-Astra-antivirus
[🇺🇸 English](README_EN.md) | **🇷🇺 Русский**

# 🛡️ StarAstra Antivirus

> **Мощная гибридная защита. Легкий вес. Полный контроль.**

**StarAstra** — это антивирус нового поколения для Windows, разработанный командой **StarAnonProject**. Мы взяли за основу проверенный open-source движок *Sentinel* и полностью переосмыслили его пользовательский опыт, превратив в мощный гибрид классического антивируса и легковесного EDR (Endpoint Detection and Response).

Проект создан для тех, кому нужна реальная защита без лишнего шума, тормозов системы и «слежки» телеметрии.

---

## 🚀 Почему StarAstra? Наши главные преимущества

*   **🧠 Легкий поведенческий анализ (EDR)**
    Классические антивирусы ищут сигнатуры файлов, мы же смотрим на *действия*. Если Word попытается скрытно запустить PowerShell или `certutil` начнет качать файлы из сети — StarAstra мгновенно **заморозит** подозрительный процесс. 
    *В отличие от многих EDR, мы не убиваем процессы сразу — мы даем вам выбор: [Убить] или [Разрешить].*
*   **🛡️ Уникальный Anti-Freeze Guard**
    Главная проблема сырых движков — ложные срабатывания, из-за которых могут зависнуть браузер или `explorer.exe`. StarAstra имеет встроенный белый список из 18 критических системных процессов. Наш предохранитель гарантирует, что антивирус **никогда не повесит систему**, даже если движок сойдет с ума.
*   **⚡ Минимальная нагрузка (Zero-Drag)**
    В режиме простоя потребление CPU составляет **~0%**, оперативной памяти — минимум. Мы не используем тяжелые нейросети в фоне и не нагружаем диск.
*   **💚 Красивый и современный UI**
    Нативный интерфейс с плавными анимациями, поддержкой темной темы (в фирменных зеленых тонах), поддержкой **RU/EN языков** и удобным треем (сворачивание в системный лоток).
*   **⚙️ Полная автономность**
    Встроенный **Watchdog**: если движок антивируса упадет, он автоматически перезапустится через 2 секунды. Автозапуск при входе в систему, планировщик сканирований и запуск в один клик.
*   **🔍 Прозрачность**
    Честный интерфейс: мы отключаем нерабочие модули (вроде сканирования памяти), чтобы не создавать ложного чувства безопасности.

---

## 📦 Установка и запуск

### Для пользователей (Готовый установщик)
1. Перейди в раздел **[Releases](../../releases)** справа.
2. Скачай `StarAstra-Setup-1.0.0.exe` (установщик) или `StarAstra-Portable-1.0.0.zip` (портативная версия).
3. Запусти и следуй инструкциям.

> ⚠️ **Важно для Windows:** Так как установщик не имеет платной цифровой подписи, Windows SmartScreen или ваш браузер могут ругаться на файл. Нажмите *"Подробнее" -> "Выполнить в любом случае"*. Это безопасно.

### Для разработчиков (Из исходного кода)
Если вы хотите запустить проект из исходников, вам понадобится Python 3.12.
1. Склонируйте репозиторий:
   ```bash
   git clone https://github.com/StarAnonProject/StarAstra.git
   cd StarAstra
Создайте виртуальное окружение и установите зависимости:

bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
Важно: Из-за ограничений лицензии бинарники движка Sentinel не входят в репозиторий. Скачайте оригинальный движок Sentinel Antivirus и поместите папку sentinel_pkg/ в корень проекта.

Запустите лаунчер:

bash
python starastra_launch.py
🧪 Как проверить работу?
1. Тест классической детекции (EICAR):
Создайте файл eicar.com на рабочем столе и вставьте в него эту строку:
X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*
Антивирус должен мгновенно отправить его в карантин и показать уведомление.

2. Тест поведенческого анализа (EDR):
Откройте командную строку (cmd) и введите:
powershell -enc ZWNobyAiaGVsbG8i
StarAstra должен распознать подозрительную цепочку (запуск скрытого PowerShell), заморозить этот процесс и выдать карточку с предложением его убить.

🛠️ Известные ограничения (v1.0)
Мы стремимся к совершенству, но зависим от open-source базы:

Движок Sentinel: Текущая версия движка не поддерживает эффективное сканирование памяти (Memory Scan) и Canary-файлы. Эти модули отключены в интерфейсе.

Ложные срабатывания: Движок может изредка реагировать на сетевую активность, но наш Anti-Freeze Guard гарантирует, что системные процессы (браузер, проводник) не будут заморожены.

🤝 Благодарности и Лицензия
Движок: Sentinel Antivirus (MIT License).

Разработка UI, Behavior Engine и Anti-Freeze Guard: [StarAnonProject].

Этот проект распространяется под лицензией MIT. Используйте, модифицируйте и улучшайте на здоровье!

text

---

### 🇺🇸 Английская версия (для файла `README_EN.md`)

```markdown
**🇺🇸 English** | [🇷🇺 Русский](README.md)

# 🛡️ StarAstra Antivirus

> **Powerful hybrid protection. Lightweight. Total control.**

**StarAstra** is a next-generation antivirus for Windows developed by the **StarAnonProject** team. We took the proven open-source *Sentinel* engine as a foundation and completely reimagined its user experience, turning it into a powerful hybrid of a classic antivirus and a lightweight EDR (Endpoint Detection and Response).

This project is built for those who need real protection without the noise, system lag, or telemetry tracking.

---

## 🚀 Why StarAstra? Our Key Advantages

*   **🧠 Lightweight Behavioral Analysis (EDR)**
    Classic antiviruses look for file signatures; we look at *actions*. If Word tries to stealthily launch PowerShell, or `certutil` starts downloading files from the network, StarAstra instantly **suspends** the suspicious process. 
    *Unlike many EDRs, we don't kill processes immediately — we give you the choice: [Kill] or [Allow].*
*   **🛡️ Unique Anti-Freeze Guard**
    The main issue with raw engines is false positives that can freeze your browser or `explorer.exe`. StarAstra has a built-in whitelist of 18 critical system processes. Our safeguard guarantees the antivirus **will never freeze your system**, even if the engine goes rogue.
*   **⚡ Minimal Load (Zero-Drag)**
    In idle mode, CPU usage is **~0%**, and RAM consumption is minimal. We don't run heavy neural networks in the background and don't thrash your disk.
*   **💚 Beautiful and Modern UI**
    A native interface with smooth animations, dark theme support (in signature green colors), **RU/EN language** support, and a convenient system tray (minimize to tray).
*   **⚙️ Full Autonomy**
    Built-in **Watchdog**: if the antivirus engine crashes, it restarts automatically in 2 seconds. Startup on boot, scan scheduler, and one-click launch.
*   **🔍 Transparency**
    Honest interface: we disable non-working modules (like memory scanning) to avoid creating a false sense of security.
---

## 📦 Installation and Launch

### For Users (Ready Installer)
1. Go to the **[Releases](../../releases)** section on the right.
2. Download `StarAstra-Setup-1.0.0.exe` (installer) or `StarAstra-Portable-1.0.0.zip` (portable version).
3. Run and follow the instructions.

> ⚠️ **Important for Windows:** Since the installer does not have a paid digital signature, Windows SmartScreen or your browser may flag the file. Click *"More info" -> "Run anyway"*. It is safe.

### For Developers (From Source Code)
If you want to run the project from source, you will need Python 3.12.
1. Clone the repository:
   ```bash
   git clone https://github.com/StarAnonProject/StarAstra.git
   cd StarAstra
Create a virtual environment and install dependencies:

bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
Important: Due to license restrictions, the Sentinel engine binaries are not included in the repository. Download the original Sentinel Antivirus engine and place the sentinel_pkg/ folder in the project root.

Run the launcher:

bash
python starastra_launch.py
🧪 How to Test?
1. Classic Detection Test (EICAR):
Create an eicar.com file on your desktop and paste this string into it:
X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*
The antivirus should instantly move it to quarantine and show a notification.

2. Behavioral Analysis Test (EDR):
Open the command prompt (cmd) and type:
powershell -enc ZWNobyAiaGVsbG8i
StarAstra should recognize the suspicious chain (stealth PowerShell launch), suspend this process, and display a card suggesting to kill it.

🛠️ Known Limitations (v1.0)
We strive for perfection, but we depend on the open-source foundation:

Sentinel Engine: The current engine version does not support efficient memory scanning or Canary files. These modules are disabled in the interface.

False Positives: The engine may occasionally react to network activity, but our Anti-Freeze Guard guarantees that system processes (browser, explorer) will not be frozen.

🤝 Credits and License
Engine: Sentinel Antivirus (MIT License).

UI, Behavior Engine, and Anti-Freeze Guard Development: [StarAnonProject].

This project is distributed under the MIT License. Use, modify, and improve it freely!
