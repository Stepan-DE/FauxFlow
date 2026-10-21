PRAGMA foreign_keys = ON;

BEGIN TRANSACTION;

-- Справочник категорий
CREATE TABLE category (
    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE
);

-- правочник подкатегорий
CREATE TABLE subcategory (
    subcategory_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id    INTEGER NOT NULL,
    name           TEXT NOT NULL,
    weight         REAL NOT NULL CHECK (weight >= 0),
    type           TEXT NOT NULL CHECK (type IN ('continuous', 'discrete')),
    config_json    TEXT NOT NULL,
    FOREIGN KEY (category_id) REFERENCES category(category_id)
);

-- Пул пользователей
CREATE TABLE users (
    user_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fio             TEXT NOT NULL,
    birth_date      TEXT NOT NULL,
    phone           TEXT NOT NULL UNIQUE,
    email           TEXT NOT NULL UNIQUE,
    register_date   TEXT NOT NULL,
    passport_number TEXT NOT NULL UNIQUE,
    address         TEXT NOT NULL,
    account_number  TEXT NOT NULL UNIQUE,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

-- Пул мерчантов
CREATE TABLE merchants (
    merchant_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    subcategory_id INTEGER NOT NULL,
    address        TEXT NOT NULL,
    phone          TEXT NOT NULL UNIQUE,
    email          TEXT NOT NULL UNIQUE,
    register_date  TEXT NOT NULL,
    weight         REAL NOT NULL CHECK (weight >= 0),
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    FOREIGN KEY (subcategory_id) REFERENCES subcategory(subcategory_id)
);

-- Расписание активности
CREATE TABLE activity_schedule (
    day           TEXT NOT NULL CHECK (day IN (
        'monday', 'tuesday', 'wednesday', 'thursday',
        'friday', 'saturday', 'sunday'
    )),
    hour          INTEGER NOT NULL CHECK (hour >= 0 AND hour < 24),
    supermarket   REAL NOT NULL,
    restaurant    REAL NOT NULL,
    transport     REAL NOT NULL,
    entertainment REAL NOT NULL,
    health        REAL NOT NULL,
    other         REAL NOT NULL,
    PRIMARY KEY (day, hour)
);

-- Конфиги генератора
CREATE TABLE generator_config (
    "key" TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Индексы
CREATE INDEX idx_users_active_id ON users(is_active, user_id);

COMMIT;