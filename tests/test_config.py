"""Configuration tests use synthetic values and open no database connections."""
import unittest
from app.config import resolve_mysql_settings


class DatabaseConfiguration(unittest.TestCase):
    def test_local_defaults(self):
        self.assertEqual(resolve_mysql_settings({}), {"host":"127.0.0.1","port":3306,"user":"root","password":"","database":"mess"})

    def test_private_url_and_escaped_credentials(self):
        result=resolve_mysql_settings({"MYSQL_URL":"mysql://student%40example.test:Test%23%40%3AOnly@mysql.railway.internal:3306/railway"})
        self.assertEqual(result,{"host":"mysql.railway.internal","port":3306,"user":"student@example.test","password":"Test#@:Only","database":"railway"})

    def test_public_and_generic_database_urls(self):
        for key in ("MYSQL_PUBLIC_URL","DATABASE_URL"):
            with self.subTest(key=key):
                result=resolve_mysql_settings({key:"mysql://tester:test-only@db.example.test:30569/railway"})
                self.assertEqual((result["host"],result["port"],result["database"]),("db.example.test",30569,"railway"))

    def test_railway_individual_variables(self):
        result=resolve_mysql_settings({"MYSQLHOST":"mysql.railway.internal","MYSQLPORT":"3306","MYSQLUSER":"tester","MYSQLPASSWORD":"test-only","MYSQLDATABASE":"railway"})
        self.assertEqual((result["host"],result["user"],result["password"],result["database"]),("mysql.railway.internal","tester","test-only","railway"))

    def test_explicit_fields_keep_test_database_isolated(self):
        result=resolve_mysql_settings({"MYSQL_URL":"mysql://tester:test-only@db.example.test/railway","MYSQL_HOST":"127.0.0.1","MYSQL_PORT":"3307","MYSQL_DATABASE":"smartmess_test_isolated"})
        self.assertEqual((result["host"],result["port"],result["database"]),("127.0.0.1",3307,"smartmess_test_isolated"))

    def test_canonical_names_override_aliases(self):
        result=resolve_mysql_settings({"MYSQLHOST":"alias.example.test","MYSQL_HOST":"canonical.example.test","MYSQLDATABASE":"alias","MYSQL_DATABASE":"canonical"})
        self.assertEqual((result["host"],result["database"]),("canonical.example.test","canonical"))

    def test_invalid_urls_never_echo_passwords(self):
        for url in ("postgres://tester:private-test-secret@db.example.test/railway","mysql://tester:private-test-secret@db.example.test:not-a-port/railway","mysql:///railway"):
            with self.assertRaises(ValueError) as error:
                resolve_mysql_settings({"MYSQL_URL":url})
            self.assertNotIn("private-test-secret",str(error.exception))

    def test_invalid_ports_are_rejected(self):
        for port in ("invalid","0","65536"):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    resolve_mysql_settings({"MYSQL_PORT":port})


if __name__ == '__main__':
    unittest.main()
