r'''
Give a user a test password in YOUR LOCAL database.sqlite (only for testing on my computer).
Useful to log in as the buddy of a newcomer, whose real password I do not know.

    .\.venv\Scripts\python.exe set_test_password.py WhiteRabbit test123

It uses the same password hashing as the signup page (werkzeug), so the normal login works.
'''
import sqlite3
import sys

from werkzeug.security import generate_password_hash


def main():
    if len(sys.argv) != 3:
        print('Usage: python set_test_password.py <username> <new password>')
        return
    username, password = sys.argv[1], sys.argv[2]

    db = sqlite3.connect('database.sqlite')
    cur = db.execute('UPDATE users SET password = ? WHERE username = ?',
                     (generate_password_hash(password), username))
    db.commit()
    db.close()

    if cur.rowcount:
        print(f'OK: @{username} can now log in with the password "{password}".')
    else:
        print(f'No user called @{username}.')


if __name__ == '__main__':
    main()
