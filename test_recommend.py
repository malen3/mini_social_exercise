r'''
Test of recommend() (Coding Assignment #3, subtask 2).

It NEVER touches the real database.sqlite: it copies it to a temporary file
and makes app.py use that copy. Run it from the project folder:

    .\.venv\Scripts\python.exe test_recommend.py
'''
import os
import shutil
import tempfile
import warnings

warnings.simplefilter('ignore')   # hide a warning of the cryptography library

import app as A

DIY_POSTS = [1810, 1875, 1880, 2113]
DIY_USERS = ['starboy99', 'DancingDolphin', 'blogger_bob']


def short(text, length=70):
    '''The text in one line, cut to `length` characters.'''
    text = ' '.join((text or '').split())
    return text if len(text) <= length else text[:length - 3] + '...'


def create_user(username):
    '''A new test user, created "now"; returns its id.'''
    cur = A.query_db('INSERT INTO users (username, password, profile) VALUES (?, ?, ?)',
                     (username, 'x', 'test user'), commit=True)
    return cur.lastrowid


def show_recommendations(title, user_id, filter_following):
    '''Print the 5 recommendations and, if the user is not a cold start, the score of each one.'''
    print()
    print('=' * 100)
    print(title)
    print('=' * 100)
    scored = A.score_candidates(user_id, filter_following)
    scores = {post['id']: score for score, post in scored} if scored else {}
    posts = A.recommend(user_id, filter_following)
    if not posts:
        print('  (no recommendations)')
    for post in posts:
        score = scores.get(post['id'])
        score_text = f'{score:5.2f}' if score is not None else ' cold'
        print(f"  score {score_text} | post {post['id']:5} | @{post['username']:<15} | {short(post['content'])}")


def main():
    # 1. A temporary copy of the database
    temp_dir = tempfile.mkdtemp()
    A.DATABASE = os.path.join(temp_dir, 'test_copy.sqlite')
    shutil.copy('database.sqlite', A.DATABASE)

    try:
        with A.app.app_context():
            # a) + b) A new user who likes the 4 DIY posts
            diy_fan = create_user('test_diy_fan')
            for post_id in DIY_POSTS:
                A.query_db("INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, 'like')",
                           (post_id, diy_fan), commit=True)

            # The interest profile that the algorithm sees
            liked = A.query_db('SELECT content FROM posts WHERE id IN (1810, 1875, 1880, 2113)')
            counts = A.collections.Counter()
            for post in liked:
                counts.update(A.extract_words(post['content']))
            print('Topics of the DIY fan (word: times):')
            print('  ' + ', '.join(f'{word}: {n}' for word, n in counts.most_common(A.TOPIC_COUNT)))

            # c) Recommendations for everybody
            show_recommendations('c) DIY fan, filter_following = False', diy_fan, False)

            # d) Following the 3 DIY users, only posts of followed users
            for username in DIY_USERS:
                row = A.query_db('SELECT id FROM users WHERE username = ?', (username,), one=True)
                if row:
                    A.query_db('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                               (diy_fan, row['id']), commit=True)
                else:
                    print(f'  (user @{username} does not exist)')
            show_recommendations('d) DIY fan following @starboy99, @DancingDolphin, @blogger_bob, filter_following = True',
                                 diy_fan, True)

            # e) Cold start: a new user without reactions or follows
            newbie = create_user('test_newbie')
            show_recommendations('e) Cold start: new user, filter_following = False', newbie, False)
            show_recommendations('e) Cold start: new user, filter_following = True (follows nobody)', newbie, True)
            show_recommendations('e) Not logged in (user_id = None)', None, False)
    finally:
        # The copy is deleted at the end: the real database is never changed
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
