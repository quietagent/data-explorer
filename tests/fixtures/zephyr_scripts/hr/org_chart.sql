-- Statement 1: a self-join with LEFT JOIN.
SELECT e.first_name, e.last_name, m.first_name AS manager_first_name
FROM employee e
LEFT JOIN employee m ON m.employee_id = e.supervisor_id;

-- Statement 2: a CTE followed back to its table, and two UNION branches.
WITH managers AS (
    SELECT employee_id, department_id FROM employee WHERE supervisor_id IS NULL
)
SELECT d.name, mg.employee_id
FROM department d
JOIN managers mg ON mg.department_id = d.department_id
UNION
SELECT s.name, e.employee_id
FROM store s
JOIN employee e ON e.store_id = s.store_id;

-- Statement 3: first_name is a column of customer and of employee, so its table is unknown.
SELECT first_name, c.email
FROM customer c
JOIN employee e ON e.email = c.email;
