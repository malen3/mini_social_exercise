from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import re
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

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

    #  4. Render Template with Pagination Info
    return render_template('feed.html.j2',
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

    # Get content from the submitted form
    content = request.form.get('content')

    # Pass the user's content through the moderation function
    moderated_content = content

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
                   (user_id, moderated_content))
        db.commit()
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

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        # Weekly Challenge: remember the progress before this comment (to know later if it counted)
        challenge = get_active_challenge(user_id)
        progress_before = challenge_progress(challenge) if challenge else 0

        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
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

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

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
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
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
    
    score = 0

    return score;

    
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

    moderated_content = content
    score = 0
    
    return moderated_content, score

# Coding Assignment #3
# Assignment 3.1
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

    recommended_posts = {} 

    return recommended_posts;




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
    requests = query_db('''
        SELECT h.id, h.post_id, h.reason, p.content, u.username AS author
        FROM help_requests h
        JOIN posts p ON h.post_id = p.id
        JOIN users u ON p.user_id = u.id
        WHERE h.helper_id = ? AND h.status = 'pending'
        ORDER BY h.created_at DESC
    ''', (user_id,)) or []

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
                           min_length=IMPACT_MIN_COMMENT_LENGTH)





if __name__ == '__main__':
    app.run(debug=True, port=8080)

