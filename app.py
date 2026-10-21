from flask import Flask, render_template, request, redirect, url_for, session, flash, g, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import collections
import json
import sqlite3
import hashlib
import re
import random
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'
# Escape user text in every template (the templates end in .html.j2, so Flask does not do it by itself)
app.jinja_env.autoescape = True

# Welcome Buddy (Coding Assignment #3, Chapter 5: Design Claims 18 and 20)
NEWCOMER_DAYS = 7              # a user is a newcomer during their first 7 days (the same as the risk rule of Coding #2)
BUDDY_MIN_ACCOUNT_DAYS = 30    # a buddy has been a member for at least 30 days
BUDDY_MIN_POSTS = 5            # ... and has written at least 5 posts
BUDDY_RECENT_DAYS = 30         # "recently active" = posts or comments in the last 30 days
BUDDY_POOL_SIZE = 10           # the buddy is chosen among the 10 most active candidates

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    # Welcome Buddy: the newcomer's buddy (to highlight their first comment on the newcomer's posts)
    my_buddy = get_my_buddy(current_user_id) if current_user_id and is_newcomer(current_user_id) else None
    my_buddy_id = my_buddy['id'] if my_buddy else None

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        # Welcome Buddy: highlight the buddy's first comment on the newcomer's own post
        mark_buddy_comments(comments_moderated, post_dict['user_id'], current_user_id, my_buddy_id)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    # Weekly Challenge: small progress bar above the posts (Design Claim 15)
    weekly_challenge = None
    weekly_progress = 0
    weekly_percent = 0
    weekly_label = ''
    if current_user_id:
        weekly_challenge = get_active_challenge(current_user_id)
    if weekly_challenge:
        weekly_progress = challenge_progress(weekly_challenge)
        weekly_percent = min(100, int(weekly_progress / weekly_challenge['target'] * 100))
        weekly_label = CHALLENGE_TYPES[weekly_challenge['challenge_type']]

    # Welcome Buddy: the welcome card on top of the feed (only for newcomers, until they close it)
    welcome_card = None
    if current_user_id and not session.get('welcome_card_closed'):
        welcome_card = welcome_card_data(current_user_id)

    #  4. Render Template with Pagination Info
    return render_template('feed.html.j2',
                           welcome_card=welcome_card,
                           posts=posts_data,
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           weekly_challenge=weekly_challenge,
                           weekly_progress=weekly_progress,
                           weekly_percent=weekly_percent,
                           weekly_label=weekly_label)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Tone Coach (Design Claim 31): a cooldown or a suspension stops the post BEFORE it is saved
    block = posting_block_message(user_id)
    if block:
        flash(block, 'warning')
        return redirect(url_for('feed'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Pass the user's content through the moderation function
    moderated_content = content

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        # cur = the "receipt" of the INSERT: cur.lastrowid is the id of the new post
        cur = db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
                   (user_id, moderated_content))
        db.commit()
        # Tone Coach: write it in the diary and, if it comes out censored, give a strike (Design Claim 31)
        tone_after_posting(user_id, 'post', cur.lastrowid, content, request.form.get('coach_action'))
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following)
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Welcome Buddy: highlight the buddy's first comment if the viewer is the newcomer who wrote the post
    viewer_id = session.get('user_id')
    if viewer_id == post['user_id'] and is_newcomer(viewer_id):
        my_buddy = get_my_buddy(viewer_id)
        mark_buddy_comments(comments, post['user_id'], viewer_id, my_buddy['id'] if my_buddy else None)

    # Ask an Expert: help requests for this post (shown to the author)
    help_requests = query_db('''
        SELECT h.status, u.username
        FROM help_requests h
        JOIN users u ON h.helper_id = u.id
        WHERE h.post_id = ?
        ORDER BY h.created_at
    ''', (post_id,)) or []
    has_pending = any(r['status'] == 'pending' for r in help_requests)
    can_ask_expert = False
    if session.get('user_id') == post['user_id'] and not comments and not has_pending:
        can_ask_expert = True

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           help_requests=help_requests,
                           can_ask_expert=can_ask_expert)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # Welcome Buddy (Design Claim 20): give the newcomer a buddy right away
            # (before db.close() below; if anything fails, the signup still works)
            try:
                assign_buddy(new_user_id)
            except Exception as e:
                print(f'Welcome Buddy: could not assign a buddy: {e}')

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Tone Coach (Design Claim 31): a cooldown or a suspension stops the comment BEFORE it is saved
    block = posting_block_message(user_id)
    if block:
        flash(block, 'warning')
        return redirect(request.referrer or url_for('post_detail', post_id=post_id))

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        # Weekly Challenge: remember the progress before this comment (to know later if it counted)
        challenge = get_active_challenge(user_id)
        progress_before = challenge_progress(challenge) if challenge else 0

        # cur = the "receipt" of the INSERT: cur.lastrowid is the id of the new comment
        cur = db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        # Tone Coach: write it in the diary and, if it comes out censored, give a strike (Design Claim 31)
        tone_after_posting(user_id, 'comment', cur.lastrowid, content, request.form.get('coach_action'))
        # Welcome Buddy: if this is the buddy's first comment on their newcomer's post, remember when
        mark_buddy_welcome(user_id, post_id)
        flash('Your comment was added.', 'success')
        # Ask an Expert: if this user had a pending help request for this post, mark it as accepted
        cur = query_db("UPDATE help_requests SET status = 'accepted' WHERE post_id = ? AND helper_id = ? AND status = 'pending'", (post_id, user_id), commit=True)
        if cur is not None and cur.rowcount > 0:
            flash('Thanks for helping!', 'success')

        # Weekly Challenge: feedback right after the comment (Design Claim 15)
        if challenge:
            progress_after = challenge_progress(challenge)
            if progress_after > progress_before:   # the new comment counted
                flash(f"Challenge progress: {progress_after} / {challenge['target']}", 'success')
                if update_challenge_status(challenge) == 'completed':
                    flash('Challenge completed! 🎉', 'success')
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

# Risk thresholds, shared by /admin and the Tone Coach
# (moved out of admin_dashboard so both use exactly the same rules)
RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }

def get_risk_profile(score):
    """Turn a risk score into (label, sort_key): >=5 HIGH, >=3 MEDIUM, >=1 LOW, otherwise NONE."""
    if score >= RISK_LEVELS["HIGH"]:
        return "HIGH", 3
    elif score >= RISK_LEVELS["MEDIUM"]:
        return "MEDIUM", 2
    elif score >= RISK_LEVELS["LOW"]:
        return "LOW", 1
    return "NONE", 0

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    PAGE_SIZE = 50

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    # Tone Coach (Design Claim 31): active strikes of every user with ONE query ({user_id: number})
    active_strikes = {row['user_id']: row['n'] for row in
                      (query_db('SELECT user_id, COUNT(*) AS n FROM strikes WHERE created_at >= ? GROUP BY user_id',
                                (strikes_window_start(),)) or [])}
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        # Tone Coach: the user's level (emoji + label) and number of active strikes
        strikes = active_strikes.get(user_dict['id'], 0)
        user_dict['standing'] = dict(STANDING_LEVELS[min(strikes, MAX_STRIKES)], strikes=strikes)
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below
# Coding Assignment #2

# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    # Read the user's profile text and the date the account was created
    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if user is None:
        return 0.0

    # Step 1: profile score
    _, profile_score = moderate_content(user['profile'])

    # Step 2: average post score (0 if the user has no posts)
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,)) or []
    average_post_score = 0.0
    if posts:
        total = 0.0
        for post in posts:
            _, post_score = moderate_content(post['content'])
            total = total + post_score
        average_post_score = total / len(posts)

    # Step 3: average comment score (0 if the user has no comments)
    average_comment_score = 0.0
    comments = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,)) or []
    if comments:
        total = 0.0
        for comment in comments:
            _, comment_score = moderate_content(comment['content'])
            total = total + comment_score
        average_comment_score = total / len(comments)

    # Step 4: combine the scores (posts count three times)
    content_risk_score = (profile_score * 1) + (average_post_score * 3) + (average_comment_score * 1)

    # Step 5: newer accounts are riskier
    now = datetime.now(timezone.utc).replace(tzinfo=None)   # current time in UTC, like created_at
    age_days = (now - user['created_at']).days if user['created_at'] else 9999
    user_risk_score = content_risk_score
    # Less than 7 days is checked first (a 3-day-old account is also "less than 30 days")
    if age_days < 7:
        user_risk_score = user_risk_score * 1.5
    elif age_days < 30:
        user_risk_score = user_risk_score * 1.2

    # Step 6: the final score is capped at 5.0
    return min(user_risk_score, 5.0)


# Pattern that detects a link (http/https, www. or a domain with a common ending).
# It is a constant so moderate_content() and the Tone Coach use exactly the same rule.
URL_PATTERN = r'(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(?:com|net|org|io|ly|co|info|biz|me)\b(?:/\S*)?)'

def has_too_many_capitals(text):
    '''
    Rule 1.2.3 (used by moderate_content() and by the Tone Coach).
    Returns True if the text has more than 15 letters and more than 70% of them are uppercase.
    '''
    letters = 0       # how many letters the text has
    uppercase = 0     # how many of them are uppercase
    for ch in text:
        if ch.isalpha():
            letters = letters + 1
            if ch.isupper():
                uppercase = uppercase + 1
    # More than 15 letters AND more than 70% uppercase (letters > 15 is checked first, so we never divide by 0)
    return letters > 15 and uppercase / letters > 0.7

# Assignment 2.1
def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    # Nothing to moderate (for example, an empty profile)
    if not content:
        return content, 0.0

    # Rule 1.1.1: Tier 1 words (severe violations) -> remove the whole content, score 5.0
    # (\b = word boundary, so only whole words count; IGNORECASE = upper/lower case does not matter)
    for word in TIER1_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', content, re.IGNORECASE):
            return ('[content removed due to severe violation]', 5.0)

    # Rule 1.1.2: Tier 2 phrases (spam/scam) -> remove the whole content, score 5.0
    # (only checked if there was no Tier 1 match, because the Tier 1 loop returns first)
    for phrase in TIER2_PHRASES:
        if re.search(r'\b' + re.escape(phrase) + r'\b', content, re.IGNORECASE):
            return ('[content removed due to spam/scam policy]', 5.0)

    # Start with the original text and a score of 0 (Stage 1.2: scored violations)
    moderated_content = content
    score = 0.0

    # Rule 1.2.1: Tier 3 words (mild profanity) -> replace with asterisks, +2.0 for each match
    for word in TIER3_WORDS:
        pattern = r'\b' + re.escape(word) + r'\b'
        matches = re.findall(pattern, moderated_content, re.IGNORECASE)
        score = score + 2.0 * len(matches)
        moderated_content = re.sub(pattern, '*' * len(word), moderated_content, flags=re.IGNORECASE)

    # Rule 1.2.2: external links -> replace with [link removed], +2.0 for each link
    matches = re.findall(URL_PATTERN, moderated_content, re.IGNORECASE)
    score = score + 2.0 * len(matches)
    moderated_content = re.sub(URL_PATTERN, '[link removed]', moderated_content, flags=re.IGNORECASE)

    # Rule 1.2.3: excessive capitalization -> +0.5 (the text is not changed)
    if has_too_many_capitals(content):
        score = score + 0.5

    return moderated_content, score

# Coding Assignment #3
# Assignment 3.1

# ----- Settings of the recommendation algorithm -----

# Reactions that mean "I like this" ('angry' and 'sad' do not count).
# The old data in the database uses 'haha', but new reactions from the buttons
# are saved as 'laugh' (see REACTION_EMOJIS), so both are here.
POSITIVE_REACTIONS = ('like', 'love', 'haha', 'laugh', 'wow')

# Very common English words that say nothing about the topic of a post
STOPWORDS = {
    'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'any', 'can', 'had', 'her', 'was',
    'one', 'our', 'out', 'has', 'his', 'how', 'its', 'let', 'who', 'did', 'yes', 'get', 'got',
    'him', 'she', 'too', 'use', 'way', 'this', 'that', 'with', 'have', 'from', 'they', 'will',
    'your', 'what', 'when', 'just', 'like', 'some', 'them', 'than', 'then', 'there', 'their',
    'about', 'would', 'could', 'should', 'which', 'these', 'those', 'been', 'were', 'into',
    'more', 'most', 'much', 'very', 'also', 'only', 'over', 'such', 'here', 'where', 'while',
    'after', 'before', 'again', 'because', 'being', 'does', 'doing', 'each', 'other', 'really',
    'today', 'know', 'think', 'make', 'made', 'going', 'want', 'need', 'still', 'even', 'every',
}

# The numbers of the algorithm
RECOMMEND_COUNT = 5             # how many posts to recommend
TOPIC_COUNT = 20                # how many of the user's most frequent words are their "topics"
MIN_WORD_LENGTH = 3             # shorter words are ignored
WEIGHT_TOPIC = 1.0              # points for each topic word in a post (x how important the topic is)
WEIGHT_FOLLOWED_AUTHOR = 2.0    # extra points if the user follows the author
WEIGHT_LIKED_AUTHOR = 0.5       # extra points if the user liked posts of this author before
                                # (less than one topic word, so the topic decides more than the author)
WEIGHT_POPULARITY = 0.5         # at most 0.5 points for the most popular post (only to break ties)
COLD_START_DAYS = 7             # cold start: the most popular posts of the last 7 days


def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """
    scored = score_candidates(user_id, filter_following)

    # None = cold start (not logged in, or no positive reactions and no follows yet)
    if scored is None:
        if filter_following:
            # Only posts of followed users were asked, and the user follows nobody
            return []
        return cold_start_posts()

    # The best RECOMMEND_COUNT posts (already sorted best first) ...
    best = [post for score, post in scored[:RECOMMEND_COUNT]]
    # ... shown newest first, like the rest of the feed
    best.sort(key=lambda post: post['created_at'], reverse=True)
    return best


def extract_words(text):
    '''The "topic words" of a text: lowercase, only letters, without stopwords and short words.
    Example: "Fixed the leaky sink! #DIYLife" -> ['fixed', 'leaky', 'sink', 'diylife']'''
    words = re.findall(r'[a-z]+', (text or '').lower())
    return [word for word in words if len(word) >= MIN_WORD_LENGTH and word not in STOPWORDS]


def matching_topic(word, topics):
    '''The topic this word belongs to, or None. A word matches a topic if it is the same word
    or if it STARTS with it, a very simple kind of stemming:
    "diydecor" (from #DIYDecor) -> "diy", "fixed" -> "fix", "projects" -> "project".'''
    if word in topics:
        return word
    for topic in topics:
        if word.startswith(topic):
            return topic
    return None


def score_candidates(user_id, filter_following):
    '''The core of recommend(): gives a score to every candidate post.
    Returns a list of (score, post) pairs, best first, or None for a cold start.
    (A separate function so the test script can show the scores.)'''
    # No user (not logged in): nothing is known about them -> cold start
    if not user_id:
        return None

    # "?, ?, ?, ?, ?": one ? per positive reaction, for the IN (...) of the queries
    marks = ', '.join('?' * len(POSITIVE_REACTIONS))

    # 1. The posts the user reacted to positively (one query)
    liked_posts = query_db(f'''SELECT p.user_id, p.content FROM reactions r JOIN posts p ON p.id = r.post_id
                               WHERE r.user_id = ? AND r.reaction_type IN ({marks})''',
                           (user_id, *POSITIVE_REACTIONS)) or []

    # 2. Interest profile: count the words of those posts; the most frequent ones are the "topics"
    word_counts = collections.Counter()
    for post in liked_posts:
        word_counts.update(extract_words(post['content']))
    topics = dict(word_counts.most_common(TOPIC_COUNT))   # {'diy': 4, 'project': 2, ...}
    top_count = max(topics.values()) if topics else 1     # the count of the most important topic

    # 3. Trusted authors: the users they follow + the authors of the posts they liked
    followed = {row['followed_id'] for row in
                (query_db('SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,)) or [])}
    liked_authors = {post['user_id'] for post in liked_posts}

    # Nothing known yet (no likes, no follows) -> cold start
    if not liked_posts and not followed:
        return None

    # 4. Candidates: posts that are not theirs and that they have not reacted to yet,
    #    with their number of positive reactions (all in one query)
    candidates = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id,
               IFNULL(pr.positive, 0) AS positive
        FROM posts p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN (SELECT post_id, COUNT(*) AS positive FROM reactions
                   WHERE reaction_type IN ({marks}) GROUP BY post_id) pr ON pr.post_id = p.id
        WHERE p.user_id != ?
          AND p.id NOT IN (SELECT post_id FROM reactions WHERE user_id = ?)''',
        (*POSITIVE_REACTIONS, user_id, user_id)) or []

    # The most positive reactions of any candidate, to turn popularity into a number from 0 to 1
    # (some posts have ~100 reactions: without this, popularity would decide everything)
    max_positive = max([row['positive'] for row in candidates] + [1])

    # 5. The score of every candidate (in Python, no more queries)
    scored = []
    for row in candidates:
        post = dict(row)
        # "Following" filter: only posts of followed users
        if filter_following and post['user_id'] not in followed:
            continue
        score = 0.0
        # +1 for every topic word in the post, weighted by how important that topic is for the user
        # (set: a word repeated 5 times in the same post only counts once)
        for word in set(extract_words(post['content'])):
            topic = matching_topic(word, topics)
            if topic:
                score += WEIGHT_TOPIC * topics[topic] / top_count
        # Known authors
        if post['user_id'] in followed:
            score += WEIGHT_FOLLOWED_AUTHOR
        if post['user_id'] in liked_authors:
            score += WEIGHT_LIKED_AUTHOR
        # A small extra for popular posts, only to break ties (0 to WEIGHT_POPULARITY points)
        score += WEIGHT_POPULARITY * post.pop('positive') / max_positive
        scored.append((round(score, 2), post))

    # Best first; with the same score, the newest first
    scored.sort(key=lambda pair: (pair[0], pair[1]['created_at']), reverse=True)
    return scored


def cold_start_posts():
    '''Cold start (new user or not logged in): the most popular posts of the last COLD_START_DAYS days.
    If there are not enough recent ones, the list is completed with the most popular posts of all time.'''
    marks = ', '.join('?' * len(POSITIVE_REACTIONS))
    since = (datetime.now(timezone.utc) - timedelta(days=COLD_START_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
    query = f'''
        SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id, COUNT(r.id) AS positive
        FROM posts p
        JOIN users u ON u.id = p.user_id
        JOIN reactions r ON r.post_id = p.id AND r.reaction_type IN ({marks})
        WHERE p.created_at >= ?
        GROUP BY p.id
        ORDER BY positive DESC, p.created_at DESC
        LIMIT ?'''
    recent = query_db(query, (*POSITIVE_REACTIONS, since, RECOMMEND_COUNT)) or []
    # All time ("since" = the beginning of time)
    all_time = query_db(query, (*POSITIVE_REACTIONS, '0000-01-01', RECOMMEND_COUNT * 2)) or []

    posts = []
    seen = set()
    for row in list(recent) + list(all_time):
        if row['id'] not in seen and len(posts) < RECOMMEND_COUNT:
            seen.add(row['id'])
            post = dict(row)
            post.pop('positive')
            posts.append(post)
    # Newest first, like the rest of the feed
    posts.sort(key=lambda post: post['created_at'], reverse=True)
    return posts




# ----- Feature: Ask an Expert -----
def init_feature_tables():
    '''Create the tables used by the Ask an Expert feature if they do not exist yet. '''
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS help_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    post_id INTEGER NOT NULL, 
    helper_id INTEGER NOT NULL, 
    requested_by INTEGER NOT NULL, 
    reason TEXT NOT NULL, 
    status TEXT NOT NULL DEFAULT 'pending', 
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
    ''')
    db.commit()




def extract_hashtags(text):
    '''
    Args:
        text: the content of a post. 

    Returns:
        A set with the hashtags found in the text, in lowercase and without '#'. 
        Example: 'I love #Yoga and #yoga' --> {'yoga'}
    '''
    text = text.lower()
    hashtags = re.findall(r'#(\w+)', text)
    hashtags = set(hashtags)
    return hashtags

def looks_like_spam(text):
    '''
    Args:
        text: the content of a post. 
    
    Returns:
        True if the text contains any known spam/scam phrase (TIER2_PHRASES), 
        False otherwise.
    '''
    text = text.lower()
    for phrase in TIER2_PHRASES:
        if phrase in text:
            return True
    return False

def find_expert(post_id):
    '''
    Args: 
        post_id: the ID of the post that needs an answer. 
    
    Returns:
        A tuple (user_id, reason) with the best candidate and a short text explaining why they are chosen, or None if nobody fits.

    How it works:
        1. Related posts = other posts that share at least one hashtag with this post. 
        2. Every user gets points for their activity on the related posts: 
            3 points per comment, 1 point per reaction, +2 if the user and the author follow each other (either direction). 
        3. Excluded: the author, users already asked for this post (any status), and users who already have a pending request. 
        4. The highest score wins; on a tie, the lowest user id wins. 
    '''
    post = query_db('SELECT id, user_id, content FROM posts WHERE id = ?', (post_id,), one=True)
    if not post:
        return None
    hashtags=extract_hashtags(post['content'])
    if not hashtags:
        return None
    related_ids=set()
    other_posts=query_db('SELECT id, content FROM posts WHERE id != ?', (post_id, ))
    for other in other_posts:
        other_hashtags=extract_hashtags(other['content'])
        if hashtags & other_hashtags:
            related_ids.add(other['id']) 
    excluded= {post['user_id']}
    asked = query_db('SELECT helper_id FROM help_requests WHERE post_id = ?', (post_id, ))
    for row in asked:
        excluded.add(row['helper_id'])
    pending = query_db ("SELECT helper_id FROM help_requests WHERE status = 'pending'")
    for row1 in pending:
        excluded.add(row1['helper_id'])

    # Score every user by their activity on the related posts
    scores = {}            # user_id -> points
    commented_posts = {}   # user_id -> set of related post ids they commented on
    reacted_posts = {}     # user_id -> set of related post ids they reacted to

    # 3 points per comment on a related post
    comments = query_db('SELECT user_id, post_id FROM comments')
    for comment in comments:
        uid = comment['user_id']
        if comment['post_id'] in related_ids and uid not in excluded:
            scores[uid] = scores.get(uid, 0) + 3
            if uid not in commented_posts:
                commented_posts[uid] = set()
            commented_posts[uid].add(comment['post_id'])

    # 1 point per reaction on a related post
    reactions = query_db('SELECT user_id, post_id FROM reactions')
    for reaction in reactions:
        uid=reaction['user_id']
        if reaction['post_id'] in related_ids and uid not in excluded:
            scores[uid] = scores.get(uid, 0) + 1
            if uid not in reacted_posts:
                reacted_posts[uid] = set()
            reacted_posts[uid].add(reaction['post_id'])

    # +2 if the user and the author follow each other (in either direction)
    author_id = post['user_id']
    for uid in scores:
        follows = query_db(
            'SELECT 1 FROM follows WHERE (follower_id = ? AND followed_id = ?) '
            'OR (follower_id = ? AND followed_id = ?)',
            (uid, author_id, author_id, uid), one=True)
        if follows:
            scores[uid] = scores[uid] + 2

    # Pick the user with the highest score (ties: the lowest user id wins)
    best_id = None
    best_score = 0
    for uid in sorted(scores):
        if scores[uid] > best_score:
            best_score= scores[uid]
            best_id = uid
    if best_id is None:
        return None
    tags_text= ', '.join('#' + tag for tag in sorted(hashtags))
    n_comments = len(commented_posts.get(best_id, set()))
    if n_comments > 0: 
        reason = f"You were chosen because you commented on {n_comments} posts about {tags_text}"
    else:
        n_reactions = len(reacted_posts.get(best_id, set()))
        reason = f"You were chosen because you reacted to {n_reactions} posts about {tags_text}"
    return (best_id, reason)

@app.route('/posts/<int:post_id>/ask-expert', methods=['POST'])
def ask_expert(post_id):
    """Handles the 'Ask an expert' button: finds the best helper and saves a help request. """
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to ask an expert.', 'danger')
        return redirect(url_for('login'))

    # Find the post and check that the current user is its author
    post = query_db('SELECT id, user_id, content FROM posts WHERE id = ?', (post_id,), one=True)
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))
    if post['user_id'] != user_id:
        flash('Only the author of the post can ask an expert.', 'danger')
        return redirect(url_for('post_detail', post_id=post_id))
    
    # Refuse if the post already has comments
    has_comments = query_db('SELECT 1 FROM comments WHERE post_id = ?', (post_id,), one=True)
    if has_comments:
        flash('This post already has replies.', 'info')
        return redirect(url_for('post_detail', post_id=post_id))

    # Refuse if the post looks like spam
    if looks_like_spam(post['content']):
        flash('This post looks like spam, so we cannot ask an expert.', 'warning')
        return redirect(url_for('post_detail', post_id=post_id))

    # Refuse if this post already has a pending request
    has_pending = query_db("SELECT 1 FROM help_requests WHERE post_id = ? AND status = 'pending'",
                           (post_id,), one=True)
    if has_pending:
        flash('An expert has already been asked for this post.', 'info')
        return redirect(url_for('post_detail', post_id=post_id))

    # Find the best expert and save the request
    result = find_expert(post_id)
    if result is None:
        flash('No expert found for these topics.', 'info')
        return redirect(url_for('post_detail', post_id=post_id))

    helper_id, reason = result
    db = get_db()
    db.execute('INSERT INTO help_requests (post_id, helper_id, requested_by, reason) VALUES (?, ?, ?, ?)',
               (post_id, helper_id, user_id, reason))
    db.commit()

    helper = query_db('SELECT username FROM users WHERE id = ?', (helper_id,), one=True)
    flash(f"Request sent to @{helper['username']}", 'success')
    return redirect(url_for('post_detail', post_id=post_id))

@app.route('/help-requests')
def my_help_requests():
    ''' 
    Shows the logged-in user the help requests they received and have not answered yet.
    '''
    user_id=session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to see your help requests.', 'danger')
        return redirect(url_for('login'))

    # Pending requests for this user, with the post text and the post author's name
    rows = query_db('''
        SELECT h.id, h.post_id, h.reason, p.content, u.username AS author
        FROM help_requests h
        JOIN posts p ON h.post_id = p.id
        JOIN users u ON p.user_id = u.id
        WHERE h.helper_id = ? AND h.status = 'pending'
        ORDER BY h.created_at DESC
    ''', (user_id,)) or []

    # Moderate the post text before showing it (Assignment 2: moderate_content)
    requests = []
    for row in rows:
        req = dict(row)
        req['content'], _ = moderate_content(req['content'])
        requests.append(req)

    return render_template('help_requests.html.j2', requests=requests)

@app.route('/help-requests/<int:request_id>/decline', methods=['POST'])
def decline_help_request(request_id):
    """Handles the 'Not now' button: declines the request and passes it on to the next best expert."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to answer help requests.', 'danger')
        return redirect(url_for('login'))

    # Find the request and check that it belongs to the current user
    req = query_db('SELECT id, post_id, helper_id, requested_by, status FROM help_requests WHERE id = ?',
                   (request_id,), one=True)
    if not req or req['helper_id'] != user_id:
        flash('Help request not found.', 'danger')
        return redirect(url_for('my_help_requests'))
    if req['status'] != 'pending':
        flash('This request was already answered.', 'info')
        return redirect(url_for('my_help_requests'))

    # Mark this request as declined
    db = get_db()
    db.execute("UPDATE help_requests SET status = 'declined' WHERE id = ?", (request_id,))
    db.commit()

    # Pass the request on to the next best expert
    # (find_expert already excludes everyone who was asked for this post before)
    result = find_expert(req['post_id'])
    if result is None:
        flash('No problem! There is no other expert for this post right now.', 'info')
    else:
        next_id, reason = result
        # Create a new request for the next expert (the author of the post is still the one asking)
        db.execute('INSERT INTO help_requests (post_id, helper_id, requested_by, reason) VALUES (?, ?, ?, ?)',
                   (req['post_id'], next_id, req['requested_by'], reason))
        db.commit()
        flash('No problem! We passed the request on to another expert.', 'success')

    return redirect(url_for('my_help_requests'))



with app.app_context():
    init_feature_tables()


# ----- Feature: Weekly Challenge -----

# Settings 
CHALLENGE_DURATION = timedelta(days=7)
CHALLENGE_TYPES = {
    'strangers': "Comment on posts from people I don't follow",
    'topics': "Comment on posts with hashtags that are new to me",
    'quiet': "Be the first to comment on a post",
}
CHALLENGE_TARGETS = [3, 5, 10]
MIN_COMMENT_LENGTH = 20

def utc_now():
    """Return the current time in UTC, without timezone info (like the dates SQLite gives us)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

def to_db_time(dt):
    """Convert a datetime to the text format SQLite uses: 'YYYY-MM-DD HH:MM:SS'."""
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def challenge_hashtags(text):
    '''
    Own copy of the hashtag helper, so this feature does not depend on Feature 1.

    Args:
        text: the content of a post.

    Returns:
        A set with the hashtags found in the text, in lowercase and without '#'.
        Example: 'I love #Yoga and #yoga' --> {'yoga'}
    '''
    text = text.lower()
    hashtags = re.findall(r'#(\w+)', text)
    hashtags = set(hashtags)
    return hashtags


def init_challenge_tables():
    '''Create the table used by the Weekly Challenge feature if it does not exist yet.'''
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS weekly_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    user_id INTEGER NOT NULL, 
    challenge_type TEXT NOT NULL, 
    target INTEGER NOT NULL, 
    start_date TIMESTAMP NOT NULL, 
    end_date TIMESTAMP NOT NULL,
    status TEXT NOT NULL DEFAULT 'active')
    ''')
    db.commit()

def challenge_progress(challenge):
    '''
    Args:
        challenge: a row of the weekly_challenges table.
    
    Returns: 
        The number of different posts where the user made a comment that counts for this challenge.
        Progress is never stored: it is always recalculated from the comments table.
    '''
    user_id = challenge['user_id']
    start = to_db_time(challenge['start_date'])
    end = to_db_time(challenge['end_date'])

    # The user's comments made during the challenge, with the author of each post
    comments = query_db('''
        SELECT c.id, c.post_id, c.content, p.user_id AS author_id, p.content AS post_content
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.user_id = ? AND c.created_at >= ? AND c.created_at <= ?
        ORDER BY c.id
    ''', (user_id, start, end)) or []

    # Data needed by the rule of the challenge type (prepared once, before the loop)
    challenge_type = challenge['challenge_type']
    followed = set()   # ids of the users this user follows
    if challenge_type == 'strangers':
        rows = query_db('SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,)) or []
        for row in rows:
            followed.add(row['followed_id'])

    known_tags = set()   # hashtags of posts the user commented on BEFORE the challenge
    if challenge_type == 'topics':
        rows = query_db('''
            SELECT p.content
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            WHERE c.user_id = ? AND c.created_at < ?
        ''', (user_id, start)) or []
        for row in rows:
            known_tags.update(challenge_hashtags(row['content']))

    counted_posts = set()   # a set, so each post counts only once (general rule 3)
    for comment in comments:
        long_enough = False
        not_own_post = False

        # General rule 1: the comment must be long enough (spaces at the ends do not count)
        if len(comment['content'].strip()) >= MIN_COMMENT_LENGTH:
            long_enough = True

        # General rule 2: comments on the user's own posts do not count
        if comment['author_id'] != user_id:
            not_own_post = True

        # Rule of the challenge type
        type_rule = False
        if challenge_type == 'strangers':
            # The author of the post must NOT be someone the user follows
            if comment['author_id'] not in followed:
                type_rule = True
        elif challenge_type == 'topics':
            # The post must have at least one hashtag that is new to the user
            new_tags = challenge_hashtags(comment['post_content']) - known_tags
            if new_tags:
                type_rule = True
        elif challenge_type == 'quiet':
            # The user's comment must be the first comment on that post (lowest id)
            first = query_db('SELECT MIN(id) AS first_id FROM comments WHERE post_id = ?', (comment['post_id'],), one=True)
            if first and first['first_id'] == comment['id']:
                type_rule = True

        if long_enough and not_own_post and type_rule:
            counted_posts.add(comment['post_id'])

    return len(counted_posts)


def update_challenge_status(challenge):
    '''
    Args:
        challenge: a row of the weekly_challenges table.

    Returns:
        The status of the challenge after checking it: 'active', 'completed' or 'failed'.
        If the status changed, it is also saved in the database.
    '''
    # Only active challenges can change
    if challenge['status'] != 'active':
        return challenge['status']

    progress = challenge_progress(challenge)
    new_status = 'active'

    # Completed is checked first: reaching the target wins even if the time is over
    if progress >= challenge['target']:
        new_status = 'completed'
    elif utc_now() > challenge['end_date']:
        new_status = 'failed'

    # Save the change (query_db, so the app keeps working even if the table is missing)
    if new_status != 'active':
        query_db('UPDATE weekly_challenges SET status = ? WHERE id = ?', (new_status, challenge['id']), commit=True)

    return new_status

def get_active_challenge(user_id):
    '''
    Args:
        user_id: the ID of a user.

    Returns:
        The user's active challenge (a row of weekly_challenges), or None if they have none.
        The status is checked first, so a challenge that just finished is not returned.
    '''
    # Read the user's challenge with status 'active' (one row, the newest one)
    challenge = query_db("SELECT * FROM weekly_challenges WHERE user_id = ? AND status = 'active' ORDER BY id DESC",
                         (user_id,), one=True)
    if challenge is None:
        return None

    # If the challenge has just been completed or has failed, it is not active anymore
    if update_challenge_status(challenge) != 'active':
        return None

    return challenge


def suggested_target(user_id):
    '''
    Args:
        user_id: the ID of a user.

    Returns:
        The target suggested for the user's next challenge (Design Claim 13: challenging but realistic):
        one level up after a completed challenge, one level down after a failed one,
        and the easiest target if the user never finished a challenge.
    '''
    # The last finished challenge (completed or failed)
    last = query_db("SELECT target, status FROM weekly_challenges WHERE user_id = ? AND status != 'active' ORDER BY id DESC",
                    (user_id,), one=True)
    if last is None:
        return CHALLENGE_TARGETS[0]

    # Work with the position of the target in the list: [3, 5, 10] -> positions 0, 1, 2
    position = CHALLENGE_TARGETS.index(last['target'])
    if last['status'] == 'completed':
        # One level up, unless it is already the hardest target
        if position < len(CHALLENGE_TARGETS) - 1:
            position = position + 1
    else:
        # One level down, unless it is already the easiest target
        if position > 0:
            position = position - 1

    return CHALLENGE_TARGETS[position]


def format_time_left(end_date):
    """Return the time left until end_date as a short text, e.g. '6 days 23 hours' or '2 minutes'."""
    seconds = int((end_date - utc_now()).total_seconds())
    if seconds < 60:
        return 'less than a minute'
    days = seconds // 86400             # 86400 seconds in a day
    hours = (seconds % 86400) // 3600   # 3600 seconds in an hour
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f'{days} days {hours} hours'
    if hours > 0:
        return f'{hours} hours {minutes} minutes'
    return f'{minutes} minutes'


@app.route('/challenge')
def challenge_page():
    """Shows the user's weekly challenge: its progress if one is active, otherwise the form to start one."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to see your weekly challenge.', 'danger')
        return redirect(url_for('login'))

    # The active challenge (if any), with its progress and time left (Design Claim 15: feedback)
    challenge = get_active_challenge(user_id)
    progress = 0
    percent = 0
    time_left = None
    if challenge:
        progress = challenge_progress(challenge)
        percent = min(100, int(progress / challenge['target'] * 100))   # for the progress bar (0-100)
        time_left = format_time_left(challenge['end_date'])

    # The last finished challenge (to show its result) and the suggested next target (Design Claim 13)
    last = query_db("SELECT * FROM weekly_challenges WHERE user_id = ? AND status != 'active' ORDER BY id DESC",
                    (user_id,), one=True)
    last_progress = challenge_progress(last) if last else 0

    return render_template('challenge.html.j2',
                           challenge=challenge,
                           progress=progress,
                           percent=percent,
                           time_left=time_left,
                           last=last,
                           last_progress=last_progress,
                           suggested=suggested_target(user_id),
                           challenge_types=CHALLENGE_TYPES,
                           challenge_targets=CHALLENGE_TARGETS,
                           min_length=MIN_COMMENT_LENGTH)



@app.route('/challenge/start', methods=['POST'])
def start_challenge():
    """Handles the form on the challenge page: creates a new weekly challenge for the logged-in user."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to start a challenge.', 'danger')
        return redirect(url_for('login'))

    # Read and validate the form (never trust what comes from a form)
    challenge_type = request.form.get('challenge_type')
    try:
        target = int(request.form.get('target', ''))
    except ValueError:
        target = None
    if challenge_type not in CHALLENGE_TYPES or target not in CHALLENGE_TARGETS:
        flash('Please choose a valid challenge and target.', 'warning')
        return redirect(url_for('challenge_page'))

    # Refuse if the user already has an active challenge
    if get_active_challenge(user_id):
        flash('You already have an active challenge.', 'info')
        return redirect(url_for('challenge_page'))

    # Save the new challenge (dates in UTC, in the same text format as comments.created_at)
    start = utc_now()
    end = start + CHALLENGE_DURATION
    query_db('INSERT INTO weekly_challenges (user_id, challenge_type, target, start_date, end_date) VALUES (?, ?, ?, ?, ?)',
             (user_id, challenge_type, target, to_db_time(start), to_db_time(end)), commit=True)

    flash(f"Challenge started: {CHALLENGE_TYPES[challenge_type]} ({target} times). Good luck!", 'success')
    return redirect(url_for('challenge_page'))


with app.app_context():
    init_challenge_tables()


# ----- Feature: Your Impact -----
# (No new table: this feature only reads users, posts, comments and reactions.)

# Setting: own constant, so this feature does not depend on Feature 2
IMPACT_MIN_COMMENT_LENGTH = 20


def period_start(period):
    '''
    Args:
        period: '7d', '30d' or 'all' (any other value is treated as 'all').

    Returns:
        The UTC start date of the period as text 'YYYY-MM-DD HH:MM:SS'
        (same format as SQLite CURRENT_TIMESTAMP), or None for 'all'.
    '''
    if period == '7d':
        days = 7
    elif period == '30d':
        days = 30
    else:
        return None     # 'all' or an unknown value: no start date

    start = datetime.now(timezone.utc) - timedelta(days=days)
    return start.strftime('%Y-%m-%d %H:%M:%S')


def impact_summary(user_id, since):
    '''
    Args:
        user_id: the ID of the user.
        since: start date as text 'YYYY-MM-DD HH:MM:SS', or None for all time.

    Returns:
        A dictionary with three numbers:
        - reactions_received: reactions by other users on the user's posts
          (reactions have no date, so for a period we count reactions on posts published in it)
        - replies_received: comments by other users on the user's posts, written in the period
        - comments_written: the user's own comments written in the period
    '''
    since = since or '1970-01-01 00:00:00'   # None means all time

    reactions = query_db('''
        SELECT COUNT(*) AS n
        FROM reactions r
        JOIN posts p ON r.post_id = p.id
        WHERE p.user_id = ? AND r.user_id != ? AND p.created_at >= ?
    ''', (user_id, user_id, since), one=True)

    replies = query_db('''
        SELECT COUNT(*) AS n
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE p.user_id = ? AND c.user_id != ? AND c.created_at >= ?
    ''', (user_id, user_id, since), one=True)

    written = query_db('SELECT COUNT(*) AS n FROM comments WHERE user_id = ? AND created_at >= ?',
                       (user_id, since), one=True)

    return {
        'reactions_received': reactions['n'] if reactions else 0,
        'replies_received': replies['n'] if replies else 0,
        'comments_written': written['n'] if written else 0,
    }


def impact_highlights(user_id, since):
    '''
    Args:
        user_id: the ID of the user.
        since: start date as text 'YYYY-MM-DD HH:MM:SS', or None for all time.

    Returns:
        Up to 3 highlights (the best first), each a dictionary with post_id, author and count:
        how many comments OTHER users wrote on that post after the user's first comment
        ("conversation started"). Only real highlights (count >= 1) are included (Design Claim 18).
    '''
    since = since or '1970-01-01 00:00:00'   # None means all time

    # The user's first comment on each post of OTHER people during the period, with the post author
    my_comments = query_db('''
        SELECT c.post_id, MIN(c.id) AS first_id, u.username AS author
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        JOIN users u ON p.user_id = u.id
        WHERE c.user_id = ? AND c.created_at >= ? AND p.user_id != ?
        GROUP BY c.post_id
    ''', (user_id, since, user_id)) or []

    highlights = []
    for row in my_comments:
        # Comments by OTHER users on the same post, written after the user's first comment (higher id)
        later = query_db('SELECT COUNT(*) AS n FROM comments WHERE post_id = ? AND user_id != ? AND id > ?',
                         (row['post_id'], user_id, row['first_id']), one=True)
        # Only real highlights: at least one other person continued the conversation
        if later and later['n'] >= 1:
            highlights.append({'post_id': row['post_id'], 'author': row['author'], 'count': later['n']})

    # The best highlights first, and only the top 3
    highlights.sort(key=lambda h: h['count'], reverse=True)
    return highlights[:3]

def contribution_scores(since):
    '''
    Args:
        since: start date as text 'YYYY-MM-DD HH:MM:SS', or None for all time.

    Returns:
        A dictionary {user_id: {'username': ..., 'score': ...}} for every user with at least
        one contribution in the period. Score = posts + comments that are long enough and
        not on the user's own posts. The 'admin' account is excluded.
    '''
    since = since or '1970-01-01 00:00:00'   # None means all time

    # One row per post and one row per valid comment (UNION ALL keeps every row),
    # then GROUP BY counts the rows of each user
    rows = query_db("""
        SELECT a.user_id, u.username, COUNT(*) AS score
        FROM (
            SELECT user_id FROM posts WHERE created_at >= ?
            UNION ALL
            SELECT c.user_id
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            WHERE c.created_at >= ? AND LENGTH(TRIM(c.content)) >= ? AND p.user_id != c.user_id
        ) AS a
        JOIN users u ON a.user_id = u.id
        WHERE u.username != 'admin'
        GROUP BY a.user_id
    """, (since, since, IMPACT_MIN_COMMENT_LENGTH)) or []

    # Turn the rows into a dictionary: user_id -> {'username': ..., 'score': ...}
    scores = {}
    for row in rows:
        scores[row['user_id']] = {'username': row['username'], 'score': row['score']}
    return scores

def next_to_catch(user_id, since):
    '''
    Args:
        user_id: the ID of the user.
        since: start date as text 'YYYY-MM-DD HH:MM:SS', or None for all time.

    Returns:
        (username, their_score, my_score, difference) for the user with the SMALLEST score
        that is STRICTLY HIGHER than mine (Design Claim 21: an achievable comparison, never
        the #1 or a full ranking), or None if nobody is above me.
    '''
    scores = contribution_scores(since)
    my_score = scores[user_id]['score'] if user_id in scores else 0   # no contributions -> 0

    # Look for the smallest score that is strictly higher than mine
    # (sorted by username, so on a tie the first name in alphabetical order wins)
    best = None
    for info in sorted(scores.values(), key=lambda i: i['username']):
        if info['score'] > my_score and (best is None or info['score'] < best['score']):
            best = info

    if best is None:
        return None     # nobody is above me in this period
    return (best['username'], best['score'], my_score, best['score'] - my_score)

@app.route('/impact')
def impact_page():
    '''Shows the logged-in user the real impact of their contributions (Design Claims 18 and 21).'''
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to see your impact.', 'danger')
        return redirect(url_for('login'))

    # The period from the URL: /impact?period=7d | 30d | all (anything else is treated as 'all')
    period = request.args.get('period', 'all')
    if period not in ('7d', '30d', 'all'):
        period = 'all'
    since = period_start(period)

    # The data of the page (only the logged-in user's own data)
    summary = impact_summary(user_id, since)
    highlights = impact_highlights(user_id, since)
    catch = next_to_catch(user_id, since)

    # Honest empty state (Design Claim 18): no praise and no comparison if all numbers are 0
    has_impact = any(summary.values())

    # Percentage for the "next to catch" progress bar: my score compared to theirs
    catch_percent = 0
    if catch:
        catch_percent = min(100, int(catch[2] / catch[1] * 100))   # catch = (name, their, mine, diff)

    return render_template('impact.html.j2',
                           period=period,
                           summary=summary,
                           highlights=highlights,
                           catch=catch,
                           catch_percent=catch_percent,
                           has_impact=has_impact,
                           min_length=IMPACT_MIN_COMMENT_LENGTH,
                           # Tone Coach card (Assignment 2): what the user did with the warnings
                           tone=tone_coach_summary(user_id),
                           # Welcome Buddy card (Assignment 3): the newcomers this user welcomed
                           buddy=welcome_buddy_summary(user_id))


# ----- Feature: Tone Coach + Consequences (Assignment 2, Chapter 4) -----
# Design Claim 20: remind people of the norms at the moment they act.

# One shared VADER analyzer for the whole app (creating it is slow, so I do it only once)
tone_analyzer = SentimentIntensityAnalyzer()


def get_tone(text):
    '''
    Args:
        text: the text the user is writing.

    Returns:
        (sentiment, tone): the VADER compound value (from -1 to +1) and
        'negative' (<= -0.5), 'positive' (>= 0.5) or 'neutral'.
    '''
    sentiment = tone_analyzer.polarity_scores(text)['compound']
    if sentiment <= -0.5:
        tone = 'negative'
    elif sentiment >= 0.5:
        tone = 'positive'
    else:
        tone = 'neutral'
    return sentiment, tone


def analyze_text(text):
    '''
    The "engine" of the Tone Coach (Design Claim 20): it joins moderate_content() and get_tone().

    Args:
        text: the text the user is writing (a post or a comment).

    Returns:
        A dictionary with how the text would be moderated and how its tone sounds.
    '''
    # Empty text: a neutral result, without errors
    if not text or not text.strip():
        return {'moderated': '', 'score': 0.0, 'risk_label': 'NONE', 'removed': False, 'censored': False,
                'sentiment': 0.0, 'tone': 'neutral', 'needs_warning': False, 'issues': []}

    # How the text would be moderated (the same moderation others will see)
    moderated, score = moderate_content(text)

    # The tone of the text, measured by VADER
    sentiment, tone = get_tone(text)

    # Censored = the moderation changed something (asterisks, [link removed] or the whole text removed)
    censored = moderated != text

    # The risk label of the score, with the same thresholds as /admin (the _ ignores the sort key)
    risk_label, _ = get_risk_profile(score)

    # Removed = the whole text was replaced by the Tier 1 or Tier 2 message
    removed = moderated == '[content removed due to severe violation]' or moderated == '[content removed due to spam/scam policy]'

    # The list of problems to show to the user (never the real Tier 1 / Tier 2 words, only the reason)
    issues = []
    if removed:
        # Removed by Tier 1 ("severe violation") or by Tier 2 ("spam/scam policy")
        reason = 'severe' if 'severe' in moderated else 'spam'
        issues.append({'type': 'removed', 'reason': reason})
    else:
        # Every Tier 3 word found in the original text
        for word in TIER3_WORDS:
            # All the matches of this word (whole words, any case), like in moderate_content
            found =re.findall(r'\b' + re.escape(word) + r'\b', text, re.IGNORECASE)
            for match in found:
                issues.append({'type': 'word', 'text': match})

        # Every link found in the original text (the same URL_PATTERN as moderate_content)
        links = re.findall(URL_PATTERN, text, re.IGNORECASE)
        for link in links:
            issues.append({'type': 'link', 'text': link})

        # Too many capital letters (rule 1.2.3)
        if has_too_many_capitals(text):
            issues.append({'type': 'caps'})


    # Warn the user if the text would be censored OR it sounds very negative
    needs_warning = censored or tone == 'negative'

    return {'moderated': moderated, 'score': score, 'risk_label': risk_label, 'removed': removed,
            'censored': censored, 'sentiment': sentiment, 'tone': tone, 'needs_warning': needs_warning,
            'issues': issues}


@app.route('/api/tone-check', methods=['POST'])
def tone_check():
    '''Live check for the Tone Coach panel: receives {"text": "..."} and returns analyze_text() as JSON.'''
    # Only for logged-in users (401 = "you are not logged in")
    if not session.get('user_id'):
        return jsonify({'error': 'You must be logged in.'}), 401

    # Read the text sent by the browser (if there is no JSON or no text, use an empty text)
    data = request.get_json(silent=True) or {}
    text = data.get('text') or ''

    # Analyze the text and send the result back to the browser as JSON
    return jsonify(analyze_text(text))


# ----- Consequences (Design Claim 31: graduated sanctions) -----

# Settings of the sanctions
STRIKE_EXPIRY_DAYS = 7     # a strike stops counting after 7 days
COOLDOWN_MINUTES = 10      # with 2 active strikes: one post or comment every 10 minutes
MAX_STRIKES = 3            # with 3 active strikes, posting is suspended

# The 4 levels, from 0 active strikes to 3 or more (position in the list = number of strikes)
STANDING_LEVELS = [
    {'level': 'good', 'emoji': '🟢', 'label': 'Good',
     'meaning': 'You can post and comment normally.'},
    {'level': 'warning', 'emoji': '🟡', 'label': 'Warning',
     'meaning': 'You can still post. Two more strikes and you will be restricted.'},
    {'level': 'cooldown', 'emoji': '🟠', 'label': 'Cooldown',
     'meaning': f'You can post or comment once every {COOLDOWN_MINUTES} minutes.'},
    {'level': 'suspended', 'emoji': '🔴', 'label': 'Suspended',
     'meaning': 'Posting is paused until your oldest strike expires.'},
]

def init_tone_coach_tables():
    '''Create the 2 tables of the Tone Coach if they do not exist yet.'''
    db = get_db()

    # One row every time someone publishes a post or a comment: what they did with the Tone Coach
    db.execute('''CREATE TABLE IF NOT EXISTS tone_coach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    coach_action TEXT NOT NULL,
    moderation_score REAL NOT NULL,
    sentiment REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
    ''')

    # One row every time someone publishes something that comes out censored (a "strike")
    db.execute('''CREATE TABLE IF NOT EXISTS strikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,  
    content_type TEXT NOT NULL,
    content_id INTEGER NOT NULL, 
    reason TEXT NOT NULL, 
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
    ''')
    db.commit()

with app.app_context():
    init_tone_coach_tables()


def tone_after_posting(user_id, content_type, content_id, content, coach_action):
    '''After a post or a comment is saved: write it in the diary (tone_coach_log)
    and, if the text comes out censored, give the user a strike (Design Claim 31).
    content_type is 'post' or 'comment'; content_id is the id of the new post or comment.'''
    # Only these 3 values are allowed (anyone could change the hidden field in the browser)
    if coach_action not in ('clean', 'edited', 'override'):
        coach_action = 'clean'

    # The same analysis as the live panel (moderate_content + VADER)
    result = analyze_text(content)

    # 1. The diary: one row for EVERY post or comment
    query_db('INSERT INTO tone_coach_log (user_id, content_type, coach_action, moderation_score, sentiment) VALUES (?, ?, ?, ?, ?)',
             (user_id, content_type, coach_action, result['score'], result['sentiment']), commit=True)

    # 2. The strike: ONLY if the published text comes out censored (and never for the admin)
    if result['censored'] and not is_admin_user(user_id):
        # The reason of the strike, in a few words (never the real Tier 1 / Tier 2 words)
        if result['removed']:
            reason = 'Content removed (' + result['issues'][0]['reason'] + ')'
        else:
            reasons = []
            if any(issue['type'] == 'word' for issue in result['issues']):
                reasons.append('mild profanity')
            if any(issue['type'] == 'link' for issue in result['issues']):
                reasons.append('link')
            reason = 'Censored: ' + ', '.join(reasons)

        # Save the strike
        query_db('INSERT INTO strikes (user_id, content_type, content_id, reason) VALUES (?, ?, ?, ?)',
                 (user_id, content_type, content_id, reason), commit=True)
        # Tell the user right away (transparency: they know why and how many strikes they have)
        flash(f"⚠ Your {content_type} came out censored, so you received a strike "
              f"({get_active_strikes(user_id)}/{MAX_STRIKES}). See 'My standing' in the menu.", 'warning')


def strikes_window_start():
    '''The moment from which strikes still count: now minus STRIKE_EXPIRY_DAYS.
    As text in UTC, the same format as CURRENT_TIMESTAMP in SQLite ('2026-10-17 18:30:00'),
    so SQL can compare it with created_at.'''
    start = datetime.now(timezone.utc) - timedelta(days=STRIKE_EXPIRY_DAYS)
    return start.strftime('%Y-%m-%d %H:%M:%S')


def to_datetime(value):
    '''SQLite can give a date as a datetime or as text (for example with MIN or MAX): always return a datetime.'''
    if value is None or isinstance(value, datetime):
        return value
    return datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S')


def get_active_strikes(user_id):
    '''How many strikes the user got in the last STRIKE_EXPIRY_DAYS days (older ones have expired).'''
    row = query_db('SELECT COUNT(*) AS n FROM strikes WHERE user_id = ? AND created_at >= ?',
                   (user_id, strikes_window_start()), one=True)
    return row['n'] if row else 0


def get_standing(user_id):
    '''The user's level (Design Claim 31): a copy of one of the STANDING_LEVELS, plus
    strikes (how many are active) and next_expiry (when the oldest active strike expires,
    so the level goes down; None if there are no strikes).'''
    strikes = get_active_strikes(user_id)
    # 0 -> good, 1 -> warning, 2 -> cooldown, 3 or more -> suspended (min: never past the last level)
    standing = dict(STANDING_LEVELS[min(strikes, MAX_STRIKES)])
    standing['strikes'] = strikes
    standing['next_expiry'] = None

    if strikes > 0:
        # The oldest active strike: when it is 7 days old, it expires
        row = query_db('SELECT MIN(created_at) AS oldest FROM strikes WHERE user_id = ? AND created_at >= ?',
                       (user_id, strikes_window_start()), one=True)
        if row and row['oldest']:
            standing['next_expiry'] = to_datetime(row['oldest']) + timedelta(days=STRIKE_EXPIRY_DAYS)
    return standing


def is_admin_user(user_id):
    '''The admin account never gets strikes or restrictions.'''
    row = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)
    return row is not None and row['username'] == 'admin'


def get_posting_status(user_id):
    '''The standing plus what it means RIGHT NOW (Design Claim 31):
    cooldown_seconds = seconds to wait before the next post or comment (only at "cooldown", otherwise 0),
    suspended_until  = until when posting is paused (only at "suspended", otherwise None).'''
    if is_admin_user(user_id):
        # The admin is always "good"
        status = dict(STANDING_LEVELS[0], strikes=0, next_expiry=None)
    else:
        status = get_standing(user_id)
    status['cooldown_seconds'] = 0
    status['cooldown_minutes'] = COOLDOWN_MINUTES
    status['suspended_until'] = None

    if status['level'] == 'suspended':
        status['suspended_until'] = status['next_expiry']
    elif status['level'] == 'cooldown':
        # When did the user last post or comment? (the newest date of both tables together)
        row = query_db('''SELECT MAX(created_at) AS last FROM (
                              SELECT created_at FROM posts WHERE user_id = ?
                              UNION ALL
                              SELECT created_at FROM comments WHERE user_id = ?)''',
                       (user_id, user_id), one=True)
        last = to_datetime(row['last']) if row else None
        if last:
            # Seconds since then; if it is less than COOLDOWN_MINUTES, the rest has to be waited
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            waited = (now - last).total_seconds()
            status['cooldown_seconds'] = max(0, int(COOLDOWN_MINUTES * 60 - waited))
    return status


def posting_block_message(user_id):
    '''The check that really counts, on the server (the browser can be changed, the server cannot).
    Returns a message if the user cannot post right now, or None if they can.'''
    status = get_posting_status(user_id)
    if status['suspended_until']:
        until = status['suspended_until'].strftime('%b %d, %Y %H:%M')
        return (f"🔴 Posting is paused until {until} (UTC) because you have {status['strikes']} active strikes. "
                "See 'My standing' in the menu.")
    if status['cooldown_seconds'] > 0:
        # Round up: 30 seconds left -> "1 minute"
        minutes = (status['cooldown_seconds'] + 59) // 60
        return (f"🟠 You are in a cooldown: you can post or comment again in {minutes} minute(s). "
                "See 'My standing' in the menu.")
    return None


@app.route('/standing')
def standing_page():
    '''"My standing" (Design Claim 31): the user's level, the active strikes and when they expire.
    Being open about the rules and the sanctions makes them feel fair.'''
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to see your standing.', 'danger')
        return redirect(url_for('login'))

    standing = get_posting_status(user_id)

    # The active strikes, newest first, each one with how many days are left until it expires
    rows = query_db('SELECT content_type, reason, created_at FROM strikes WHERE user_id = ? AND created_at >= ? ORDER BY created_at DESC',
                    (user_id, strikes_window_start())) or []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    strikes = []
    for row in rows:
        created = to_datetime(row['created_at'])
        expires = created + timedelta(days=STRIKE_EXPIRY_DAYS)
        # Days left, rounded up (1.2 days -> 2 days)
        days_left = max(0, -(-(expires - now).total_seconds() // 86400))
        strikes.append({'content_type': row['content_type'], 'reason': row['reason'],
                        'created_at': created, 'expires_in_days': int(days_left)})

    return render_template('standing.html.j2', standing=standing, strikes=strikes,
                           levels=STANDING_LEVELS, max_strikes=MAX_STRIKES, expiry_days=STRIKE_EXPIRY_DAYS)


def tone_coach_summary(user_id):
    '''Data for the "Tone Coach" card on the Your Impact page (from the diary, tone_coach_log):
    how many times the coach warned the user, how many times they rewrote the message ("edited")
    or posted anyway ("override"), and the average tone of their last 20 messages.'''
    # How many rows of each coach_action ({'clean': 12, 'edited': 3, 'override': 1})
    rows = query_db('SELECT coach_action, COUNT(*) AS n FROM tone_coach_log WHERE user_id = ? GROUP BY coach_action',
                    (user_id,)) or []
    counts = {row['coach_action']: row['n'] for row in rows}
    edited = counts.get('edited', 0)
    override = counts.get('override', 0)
    # Every warning ended in "edited" or in "override"
    warned = edited + override

    # The average tone (VADER) of the last 20 messages
    recent = query_db('SELECT sentiment FROM tone_coach_log WHERE user_id = ? ORDER BY id DESC LIMIT 20',
                      (user_id,)) or []
    average = round(sum(row['sentiment'] for row in recent) / len(recent), 2) if recent else 0.0
    # The same thresholds as get_tone()
    if average <= -0.5:
        tone = 'negative'
    elif average >= 0.5:
        tone = 'positive'
    else:
        tone = 'neutral'

    return {
        'warned': warned,
        'edited': edited,
        'override': override,
        'rewrite_percent': round(edited / warned * 100) if warned else 0,
        'messages': len(recent),
        'average': average,
        'tone': tone,
        # Position on the tone bar: -1 -> 0 %, 0 -> 50 %, +1 -> 100 %
        'marker_percent': round((average + 1) / 2 * 100),
    }


@app.route('/admin/strikes/<int:user_id>/clear', methods=['POST'])
def admin_clear_strikes(user_id):
    '''Lets the admin remove all the strikes of a user (for example, after a mistake of the filter).'''
    if session.get('username') != 'admin':
        flash('You do not have permission to perform this action.', 'danger')
        return redirect(url_for('feed'))
    query_db('DELETE FROM strikes WHERE user_id = ?', (user_id,), commit=True)
    flash(f'The strikes of user {user_id} have been cleared.', 'success')
    return redirect(request.referrer or url_for('admin_dashboard'))


@app.context_processor
def inject_tone_status():
    '''Makes tone_status (level, cooldown, suspension) available in EVERY template,
    so the forms can show a notice or be replaced when the user is restricted.'''
    user_id = session.get('user_id')
    if not user_id:
        return {'tone_status': None}
    return {'tone_status': get_posting_status(user_id)}


# ----- Feature: Welcome Buddy (Coding Assignment #3, Chapter 5: Dealing with Newcomers) -----
# Design Claim 18: newcomers who have friendly interactions with old members soon after joining
#                  stay longer and contribute more.
# Design Claim 20: assigning "welcomers" (old members in charge of receiving new ones) helps.
# Every new user automatically gets a trusted, active "buddy" who welcomes them and replies to their first posts.
# (The settings NEWCOMER_DAYS, BUDDY_MIN_ACCOUNT_DAYS... are at the top of app.py.)

def init_welcome_buddy_tables():
    '''Create the table of the Welcome Buddy feature if it does not exist yet.'''
    db = get_db()
    # One row per newcomer: who their buddy is, and when the buddy first interacted with them
    db.execute('''CREATE TABLE IF NOT EXISTS buddies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    newcomer_id INTEGER NOT NULL,
    buddy_id INTEGER NOT NULL,
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    welcomed_at TIMESTAMP NULL)
    ''')
    db.commit()

with app.app_context():
    init_welcome_buddy_tables()


def days_ago_text(days):
    '''The moment "now minus X days", as UTC text in the same format as CURRENT_TIMESTAMP (to compare in SQL).'''
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime('%Y-%m-%d %H:%M:%S')


def is_newcomer(user_id):
    '''True if the account is less than NEWCOMER_DAYS days old.'''
    if not user_id:
        return False
    row = query_db('SELECT 1 FROM users WHERE id = ? AND created_at >= ?',
                   (user_id, days_ago_text(NEWCOMER_DAYS)), one=True)
    return row is not None


def choose_buddy(newcomer_id):
    '''Choose a buddy for a newcomer (Design Claim 20). Returns the buddy's id, or None.

    1. SQL filter: account of BUDDY_MIN_ACCOUNT_DAYS+ days, BUDDY_MIN_POSTS+ posts, not the admin,
       not the newcomer, and no active strikes (Coding #2). The same query counts each candidate's
       recent activity, total activity and how many newcomers they have now.
    2. Most active first: recent activity (last BUDDY_RECENT_DAYS days), then total activity
       (so if nobody has been active recently, the total activity decides).
    3. Walking down that list, only users with risk NONE (user_risk_analysis < 1.0) enter the pool,
       until it has BUDDY_POOL_SIZE people (the risk is slow, so it is not computed for everybody).
    4. In the pool, the one with the fewest newcomers right now (to share the work); ties at random.'''
    candidates = query_db('''
        SELECT u.id,
               (SELECT COUNT(*) FROM posts WHERE user_id = u.id AND created_at >= ?)
             + (SELECT COUNT(*) FROM comments WHERE user_id = u.id AND created_at >= ?) AS recent,
               (SELECT COUNT(*) FROM posts WHERE user_id = u.id)
             + (SELECT COUNT(*) FROM comments WHERE user_id = u.id) AS total,
               (SELECT COUNT(*) FROM buddies b JOIN users n ON n.id = b.newcomer_id
                 WHERE b.buddy_id = u.id AND n.created_at >= ?) AS load
        FROM users u
        WHERE u.id != ?
          AND u.username != 'admin'
          AND u.created_at <= ?
          AND (SELECT COUNT(*) FROM posts WHERE user_id = u.id) >= ?
          AND u.id NOT IN (SELECT user_id FROM strikes WHERE created_at >= ?)''',
        (days_ago_text(BUDDY_RECENT_DAYS), days_ago_text(BUDDY_RECENT_DAYS), days_ago_text(NEWCOMER_DAYS),
         newcomer_id, days_ago_text(BUDDY_MIN_ACCOUNT_DAYS), BUDDY_MIN_POSTS, strikes_window_start())) or []

    # Most active first
    candidates = sorted(candidates, key=lambda row: (row['recent'], row['total']), reverse=True)

    # Only trusted people (risk NONE), until the pool is full
    pool = []
    for row in candidates:
        if user_risk_analysis(row['id']) < 1.0:
            pool.append(row)
            if len(pool) == BUDDY_POOL_SIZE:
                break
    if not pool:
        return None

    # The fewest newcomers right now; if several have the same number, one of them at random
    fewest = min(row['load'] for row in pool)
    return random.choice([row['id'] for row in pool if row['load'] == fewest])


def assign_buddy(newcomer_id):
    '''Choose a buddy, save it in the table buddies, and make the buddy follow the newcomer
    (so the newcomer's posts appear in the buddy's "Following" feed). Returns the buddy's id or None.'''
    # A newcomer only gets one buddy
    existing = query_db('SELECT buddy_id FROM buddies WHERE newcomer_id = ?', (newcomer_id,), one=True)
    if existing:
        return existing['buddy_id']

    buddy_id = choose_buddy(newcomer_id)
    if buddy_id is None:
        return None
    query_db('INSERT INTO buddies (newcomer_id, buddy_id) VALUES (?, ?)', (newcomer_id, buddy_id), commit=True)
    # The buddy follows the newcomer (only if they do not follow them already: follows has no unique rule)
    if not query_db('SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?', (buddy_id, newcomer_id), one=True):
        query_db('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)', (buddy_id, newcomer_id), commit=True)
    return buddy_id


def get_my_buddy(user_id):
    '''The buddy of this user (a dict with id, username, profile, created_at, days_here, assigned_at, welcomed_at),
    or None if they have no buddy.'''
    row = query_db('''SELECT u.id, u.username, u.profile, u.created_at, b.assigned_at, b.welcomed_at
                      FROM buddies b JOIN users u ON u.id = b.buddy_id
                      WHERE b.newcomer_id = ?''', (user_id,), one=True)
    if not row:
        return None
    buddy = dict(row)
    created = to_datetime(buddy['created_at'])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    buddy['days_here'] = (now - created).days if created else 0
    return buddy


def get_my_newcomers(user_id):
    '''The newcomers assigned to this buddy who are STILL newcomers, newest first
    (a list of dicts with id, username, profile, created_at, assigned_at, welcomed_at).'''
    rows = query_db('''SELECT n.id, n.username, n.profile, n.created_at, b.assigned_at, b.welcomed_at
                       FROM buddies b JOIN users n ON n.id = b.newcomer_id
                       WHERE b.buddy_id = ? AND n.created_at >= ?
                       ORDER BY n.created_at DESC''', (user_id, days_ago_text(NEWCOMER_DAYS))) or []
    return [dict(row) for row in rows]


def mark_buddy_welcome(buddy_id, post_id):
    '''Called by add_comment: if this comment is the buddy's FIRST interaction with their newcomer
    (a comment on one of the newcomer's posts), remember when it happened (welcomed_at).'''
    query_db('''UPDATE buddies SET welcomed_at = CURRENT_TIMESTAMP
                WHERE buddy_id = ? AND welcomed_at IS NULL
                  AND newcomer_id = (SELECT user_id FROM posts WHERE id = ?)''',
             (buddy_id, post_id), commit=True)


# --- What the NEWCOMER sees (subtask 4) ---

def time_here_text(days):
    '''How long someone has been a member, in friendly words: "12 days", "5 months", "3 years".'''
    if days < 60:
        return f'{days} day' + ('' if days == 1 else 's')
    if days < 730:
        return f'{days // 30} months'
    return f'{days // 365} years'


def newcomer_days_left(user_id, created_at=None):
    '''How many days this user is still a newcomer (0 if they are not one any more).
    If the account date (created_at) is already known, it is passed in and no query is needed.'''
    if created_at is None:
        row = query_db('SELECT created_at FROM users WHERE id = ?', (user_id,), one=True)
        created_at = row['created_at'] if row else None
    created = to_datetime(created_at)
    if not created:
        return 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    seconds_left = (created + timedelta(days=NEWCOMER_DAYS) - now).total_seconds()
    # Round up: 4.2 days left -> "5 more days"
    return max(0, int(-(-seconds_left // 86400)))


def welcome_card_data(user_id):
    '''Everything the welcome card on the feed needs (only for newcomers), or None.
    Design Claim 18: the newcomer knows from the first minute that a friendly old member is there for them.'''
    if not is_newcomer(user_id):
        return None

    # The first steps checklist, with an automatic tick (counted from the database in one query)
    counts = query_db('''SELECT (SELECT COUNT(*) FROM posts WHERE user_id = ?) AS posts,
                                (SELECT COUNT(*) FROM follows WHERE follower_id = ?) AS follows,
                                (SELECT COUNT(*) FROM reactions WHERE user_id = ?) AS reactions''',
                      (user_id, user_id, user_id), one=True)
    checklist = [
        {'label': 'Write your first post', 'done': counts['posts'] >= 1},
        {'label': 'Follow 3 people', 'done': counts['follows'] >= 3},
        {'label': 'React to a post', 'done': counts['reactions'] >= 1},
    ]

    buddy = get_my_buddy(user_id)
    if buddy:
        # The buddy's profile, moderated like everywhere else (the template escapes it too)
        buddy['profile'], _ = moderate_content(buddy['profile'] or '')
        buddy['time_here'] = time_here_text(buddy['days_here'])

    return {'days_left': newcomer_days_left(user_id), 'buddy': buddy, 'checklist': checklist,
            'steps_done': sum(1 for step in checklist if step['done'])}


def mark_buddy_comments(comments, post_author_id, viewer_id, buddy_id):
    '''If the viewer is the newcomer who wrote the post, mark the FIRST comment of their buddy
    (comment['from_buddy'] = True), so the template can highlight it. No database queries.'''
    if not buddy_id or viewer_id != post_author_id:
        return
    for comment in comments:
        if comment['user_id'] == buddy_id:
            comment['from_buddy'] = True
            return


@app.route('/welcome/dismiss', methods=['POST'])
def dismiss_welcome_card():
    '''Close the welcome card for the rest of this session.'''
    session['welcome_card_closed'] = True
    return redirect(request.referrer or url_for('feed'))


# --- What the BUDDY sees (subtask 5, Design Claim 20: the welcomer) ---

def time_ago_text(moment):
    '''How long ago something happened, in short words: "5 min ago", "3h ago", "2 days ago".'''
    moment = to_datetime(moment)
    if not moment:
        return ''
    seconds = (datetime.now(timezone.utc).replace(tzinfo=None) - moment).total_seconds()
    if seconds < 3600:
        return f'{max(1, int(seconds // 60))} min ago'
    if seconds < 86400:
        return f'{int(seconds // 3600)}h ago'
    days = int(seconds // 86400)
    return f'{days} day' + ('' if days == 1 else 's') + ' ago'


@app.route('/welcome')
def welcome_page():
    '''"Your newcomers 🌱": the buddy's page. For each newcomer: their profile, how many days they are
    still new, and their posts; the posts that nobody has answered yet have a quick comment form.'''
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to see your newcomers.', 'danger')
        return redirect(url_for('login'))

    newcomers = get_my_newcomers(user_id)
    ids = [newcomer['id'] for newcomer in newcomers]

    # All the posts of all my newcomers in ONE query, with the number of comments from OTHER users
    posts_by_user = {newcomer_id: [] for newcomer_id in ids}
    if ids:
        marks = ', '.join('?' * len(ids))
        rows = query_db(f'''SELECT p.id, p.user_id, p.content, p.created_at,
                                   (SELECT COUNT(*) FROM comments c
                                     WHERE c.post_id = p.id AND c.user_id != p.user_id) AS replies
                            FROM posts p WHERE p.user_id IN ({marks})
                            ORDER BY p.created_at DESC''', ids) or []
        for row in rows:
            post = dict(row)
            post['content'], _ = moderate_content(post['content'])
            post['ago'] = time_ago_text(post['created_at'])
            posts_by_user[post['user_id']].append(post)

    for newcomer in newcomers:
        newcomer['profile'], _ = moderate_content(newcomer['profile'] or '')
        newcomer['days_left'] = newcomer_days_left(newcomer['id'], newcomer['created_at'])
        newcomer['posts'] = posts_by_user[newcomer['id']]

    # How many newcomers I have welcomed so far (all time)
    welcomed = query_db('SELECT COUNT(*) AS n FROM buddies WHERE buddy_id = ? AND welcomed_at IS NOT NULL',
                        (user_id,), one=True)['n']

    return render_template('welcome.html.j2', newcomers=newcomers, welcomed=welcomed)


def welcome_buddy_summary(user_id):
    '''Data for the "Welcome Buddy" card on the Your Impact page: how many newcomers were given to this
    user, how many they welcomed, and how long they took on average to welcome them (in hours).'''
    row = query_db('''SELECT COUNT(*) AS assigned,
                             COUNT(welcomed_at) AS welcomed,
                             AVG((julianday(welcomed_at) - julianday(assigned_at)) * 24) AS avg_hours
                      FROM buddies WHERE buddy_id = ?''', (user_id,), one=True)
    if not row or not row['assigned']:
        return None
    avg_hours = row['avg_hours']
    if avg_hours is None:
        avg_text = None
    elif avg_hours < 1:
        minutes = max(1, round(avg_hours * 60))
        avg_text = f'{minutes} minute' + ('' if minutes == 1 else 's')
    elif avg_hours < 48:
        avg_text = f'{round(avg_hours, 1)} hours'
    else:
        avg_text = f'{round(avg_hours / 24, 1)} days'
    return {'assigned': row['assigned'], 'welcomed': row['welcomed'], 'avg_text': avg_text}


@app.context_processor
def inject_welcome_nav():
    '''For the "🌱 Welcome" link in the navigation bar (only for buddies with newcomers):
    how many active newcomers I have and how many of their posts are waiting for a reply. ONE query.'''
    user_id = session.get('user_id')
    if not user_id:
        return {'welcome_nav': None}
    try:
        row = query_db('''
            SELECT COUNT(DISTINCT n.id) AS newcomers,
                   (SELECT COUNT(*) FROM posts p
                     WHERE p.user_id IN (SELECT b2.newcomer_id FROM buddies b2 JOIN users n2 ON n2.id = b2.newcomer_id
                                          WHERE b2.buddy_id = ? AND n2.created_at >= ?)
                       AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.user_id != p.user_id)) AS waiting
            FROM buddies b JOIN users n ON n.id = b.newcomer_id
            WHERE b.buddy_id = ? AND n.created_at >= ?''',
            (user_id, days_ago_text(NEWCOMER_DAYS), user_id, days_ago_text(NEWCOMER_DAYS)), one=True)
    except Exception:
        row = None
    if not row or not row['newcomers']:
        return {'welcome_nav': None}
    return {'welcome_nav': {'newcomers': row['newcomers'], 'waiting': row['waiting']}}


@app.context_processor
def inject_newcomer_ids():
    '''newcomer_ids: the ids of ALL newcomers, in every template, with ONE query per page.
    The templates use it for the "🌱 New member" badge without one query per post.'''
    try:
        rows = query_db('SELECT id FROM users WHERE created_at >= ?', (days_ago_text(NEWCOMER_DAYS),)) or []
    except Exception:
        rows = []
    return {'newcomer_ids': {row['id'] for row in rows}}


if __name__ == '__main__':
    app.run(debug=True, port=8080)

