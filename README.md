# Nano CRM Bot

A lightweight Telegram bot CRM system designed for small businesses and individual specialists. Manage orders, track statuses, generate reports, and automate daily workflows directly from Telegram.

## Overview

Nano CRM is a Telegram-based customer relationship management bot that helps small businesses and freelancers manage their orders efficiently. It provides a simple interface for creating, tracking, and managing orders with status updates, search capabilities, automated reports, and reminder notifications.

**Main Purpose:**
- Order management and tracking
- Status workflow management (New → In Progress → Delivery → Paid/Canceled)
- Automated daily reports
- Search and filtering capabilities
- Export to PDF, Excel, and CSV formats

## Features

### Order Management
- **Create Orders**: Simple 5-field format (`model / price / address / contact / comment`)
- **Edit Orders**: Update orders by editing the original message or using inline buttons
- **Status Tracking**: 5 statuses with visual indicators:
  - 🆕 New
  - 📦 In Progress
  - 🚚 Delivery
  - ✅ Paid
  - ❌ Canceled
- **Order Cards**: Formatted display with all order details
- **Duplicate Prevention**: Automatic detection of duplicate orders

### Search & Filtering
- **Full-text Search**: Search across all order fields (model, price, address, contact, comment, manager)
- **Status Filtering**: View orders by status (last 10 shown)
- **Case-insensitive Search**: Supports both Cyrillic and Latin characters

### Reports & Export
- **PDF Reports**: Formatted reports with Cyrillic font support
- **Excel Reports**: Multi-sheet reports with formatting
- **CSV Export**: Raw data export for external analysis
- **Daily Reports**: Automated Excel reports with:
  - Summary statistics (order count, total revenue, status distribution)
  - Active orders sheet
  - All orders sheet
- **Status-based Reports**: Generate reports filtered by order status
- **Bulk Status Updates**: Mass update orders by status

### Automation
- **Daily Report Scheduler**: Automatically sends daily reports at 18:30 (configurable)
- **Reminder System**: Set reminders in comments (e.g., "завтра 15:00", "28.12 20:00")
- **Reminder Notifications**: Automatic notifications 5 minutes before scheduled time

### Access Control
- **PIN-based Authorization**: Secure access with configurable PIN code
- **User Authorization**: Persistent authorization system
- **Rate Limiting**: Protection against abuse (3-second delay between messages, 50 orders per day limit)

### Additional Features
- **Comment History**: Track all comment updates with timestamps
- **Phone Normalization**: Automatic phone number formatting
- **Message Reactions**: Visual status indicators via Telegram reactions
- **Inline Editing**: Edit specific fields (price, address, customer name, phone) via inline buttons

## Tech Stack

- **Language**: Python 3.11
- **Telegram Framework**: aiogram 3.15.0
- **Database**: SQLite (stored in `data/nano_crm.db`)
- **Key Libraries**:
  - `aiosqlite` - Async SQLite operations
  - `openpyxl` - Excel file generation
  - `reportlab` - PDF generation with Cyrillic support
  - `pydantic` - Data validation
  - `python-dotenv` - Environment variable management
- **Deployment**: Docker + Docker Compose

## Architecture

### Project Structure

```
nano_crm/
├── main.py              # Entry point, schedulers
├── config.py            # Configuration and environment variables
├── db.py                # Database operations and queries
├── models.py            # Pydantic data models
├── keyboards.py         # Reply keyboard layouts
├── handlers/
│   ├── orders.py       # Order management handlers
│   └── report.py       # Report generation handlers
├── data/                # Database storage directory
├── Dockerfile          # Docker image definition
├── docker-compose.yml  # Docker Compose configuration
└── requirements.txt    # Python dependencies
```

### Data Flow

1. **User Input** → Telegram Bot API
2. **Handler Processing** → Parse message, validate format
3. **Database Operations** → Store/retrieve data from SQLite
4. **Response Generation** → Format order cards, generate reports
5. **Bot Response** → Send formatted message/report to user

### Database Schema

**orders** table:
- `id` (PRIMARY KEY)
- `model`, `price`, `address`, `contact_raw`, `phone`, `customer_name`, `comment`
- `manager_id`, `manager_name`
- `chat_id`, `message_id`
- `created_at`, `updated_at`, `status`
- `reminder_at`, `reminder_sent`
- `comment_history`

**settings** table:
- `chat_id` (PRIMARY KEY)
- `daily_report_enabled`
- `report_chat_id`
- `last_report_date`

**authorized_users** table:
- `user_id` (PRIMARY KEY)
- `authorized`

## Setup & Run

### Prerequisites

- Python 3.11+
- Docker and Docker Compose (for containerized deployment)
- Telegram Bot Token (obtain from [@BotFather](https://t.me/BotFather))

### Local Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd nano_crm
   ```

2. **Create `.env` file**
   Create a `.env` file in the project root with the following variables:
   ```env
   TG_BOT_TOKEN=your_telegram_bot_token_here
   ```
   Replace `your_telegram_bot_token_here` with your actual bot token from BotFather.

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Initialize database**
   The database will be automatically created on first run. Ensure the `data/` directory exists:
   ```bash
   mkdir -p data
   ```

5. **Run the bot**
   ```bash
   python main.py
   ```

### Docker Setup

1. **Create `.env` file**
   Create a `.env` file in the project root (same as local setup):
   ```env
   TG_BOT_TOKEN=your_telegram_bot_token_here
   ```
   The `docker-compose.yml` automatically loads this file via `env_file: .env`.

2. **Build and run with Docker Compose**
   ```bash
   docker-compose up -d
   ```

3. **View logs**
   ```bash
   docker-compose logs -f
   ```

4. **Stop the bot**
   ```bash
   docker-compose down
   ```

**Docker Services:**
- `nano_crm`: Main bot service
  - Port mapping: `8081:8080` (HTTP health/check endpoint or future webhook support)
  - Volume: `./data:/app/data` (persistent SQLite storage)
  - Env file: `.env` (contains `TG_BOT_TOKEN`)
  - Restart policy: `always`

**Note**: The bot uses long polling by default; the exposed port 8080 is reserved for potential webhook or monitoring endpoints. Database files are persisted in the `./data` directory.

## Usage

### Getting Started

1. Start a conversation with your bot in Telegram
2. Send `/start` command
3. Enter the PIN code (default: `1234`, configured in `config.py`)
4. You'll receive a welcome message with instructions

### Creating Orders

Send a message in the following format (5 fields separated by `/`):
```
model / price / address / contact / comment
```

**Example:**
```
Цветы / 15000 / Нью-Йорк / 89991234567 Питер Паркер / завтра 15:00
```

The bot will:
- Create an order card with formatted display
- Extract and normalize phone number
- Parse reminder time from comment (if present)
- Show inline buttons for status changes

### Managing Orders

- **Change Status**: Click status emoji buttons (🆕📦🚚✅❌) under order cards
- **Edit Order**: Click ✏️ button, then select field to edit (💰📍👤📞)
- **Edit via Reply**: Reply to an order card with new comment text
- **Edit Original Message**: Edit the original order message to update all fields
- **Find Order**: Use `/find <order_id>` to view and edit specific order
- **Quick View**: Send `#<order_id>` to display order card

### Search

- **Text Search**: Simply type your search query (searches across all fields)
- **Status Filter**: Use keyboard buttons (🆕 Новые, 📦 В работе, etc.)
- **Search Button**: Click "🔍 Поиск" button for search mode

### Reports

- **PDF Report**: Click "📊 Отчёт" button or use `/report_pdf`
- **Excel Report**: Use `/report_xlsx`
- **CSV Report**: Use `/report`
- **Status Reports**: Click status button, then "📊 Сформировать отчёт"
- **Daily Reports**: Enable via "🔔 Ежедневный отчёт: ВКЛ" button

### Reminders

Add time/date in comments to set reminders:
- `завтра 15:00` - Tomorrow at 15:00 (reminder at 14:55)
- `28.12 20:00` - December 28 at 20:00 (reminder at 19:55)
- `20:00` - Today at 20:00 (if not passed) or tomorrow

Reminders are sent 5 minutes before the scheduled time.

### Commands

- `/start` - Start bot and show welcome message
- `/help` - Show help information
- `/find <id>` - Find order by ID for editing
- `/set_status <id> <status>` - Change order status
- `/report` - Generate CSV report
- `/report_pdf` - Generate PDF report
- `/report_xlsx` - Generate Excel report

## Potential Use Cases

### Micro-Business
- **Freelancers**: Track client projects, deadlines, and payments
- **Service Providers**: Manage appointments, deliveries, and customer orders
- **Small Retail**: Inventory orders, customer information, and sales tracking

### Small Teams
- **Internal CRM**: Team collaboration on customer orders within Telegram
- **Field Workers**: Quick order entry and status updates from mobile devices
- **Sales Teams**: Track leads, deals, and customer interactions

### Individual Specialists
- **Consultants**: Client project management and follow-ups
- **Artists/Craftsmen**: Commission tracking and delivery scheduling
- **Tutors/Coaches**: Session scheduling and payment tracking

## Configuration

### Environment Variables

- `TG_BOT_TOKEN` (required): Your Telegram bot token from BotFather

### Code Configuration (`config.py`)

- `BOT_PIN`: PIN code for bot access (default: `"1234"`)
- `DAILY_REPORT_HOUR`: Hour for daily report (default: `19`)
- `DAILY_REPORT_MINUTE`: Minute for daily report (default: `0`)

**Note**: Change the PIN code in `config.py` before deploying to production.

## License

[Add your license information here]

## Contributing

[Add contribution guidelines if applicable]

