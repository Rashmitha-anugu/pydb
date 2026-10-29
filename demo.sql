-- Run with:   python -m pydb shop.db   then   .read demo.sql
CREATE TABLE customers (
    id    INT PRIMARY KEY,
    name  TEXT(32) NOT NULL,
    city  TEXT(16),
    age   INT,
    spend FLOAT,
    vip   BOOL
);

INSERT INTO customers VALUES
    (1, 'Ana Silva',   'Austin',  31, 1250.50, TRUE),
    (2, 'Bob Lee',     'Boston',  45,  310.00, FALSE),
    (3, 'Chikki Rao',  'Austin',  28,  980.25, TRUE),
    (4, 'Dan O''Neil', NULL,      52,   75.00, FALSE),
    (5, 'Eve Park',    'Chicago', 23, 2210.00, TRUE),
    (6, 'Farah Khan',  'Boston',  37,  640.75, FALSE),
    (7, 'Gus Ortiz',   'Austin',  NULL, 15.00, FALSE);

SELECT * FROM customers;
SELECT name, spend FROM customers WHERE vip = TRUE ORDER BY spend DESC;
SELECT name, age FROM customers WHERE age BETWEEN 25 AND 40 AND city IS NOT NULL;
SELECT COUNT(*) FROM customers WHERE city = 'Austin';

EXPLAIN SELECT * FROM customers WHERE id = 5;
EXPLAIN SELECT * FROM customers WHERE id >= 2 AND id <= 4;
EXPLAIN SELECT * FROM customers WHERE city = 'Austin';
CREATE INDEX idx_city ON customers (city);
EXPLAIN SELECT * FROM customers WHERE city = 'Austin';
SELECT name FROM customers WHERE city = 'Austin' ORDER BY name;
