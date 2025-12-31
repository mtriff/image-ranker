from flask import Flask, render_template, request, jsonify, send_file, Response
import os
import random
import itertools
from elo import TrueSkillRanking
import csv
from io import StringIO
import threading
import logging
from datetime import datetime
import platform
import queue
import subprocess

logging.basicConfig(level=logging.DEBUG)

# Global variables
app = Flask(__name__)
elo_ranking = TrueSkillRanking()
excluded_images = set()
current_directory = None
IMAGE_FOLDER = 'static/images'
image_pairs_lock = threading.Lock()
image_pairs = []
current_pair_index = 0
last_shown_image = None
comparisons_since_autosave = 0

def get_image_paths():
    image_paths = []
    app.logger.debug(f"Searching for images in: {IMAGE_FOLDER}")
    for root, dirs, files in os.walk(IMAGE_FOLDER):
        for file in files:
            if file.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.jfif', '.avif', '.heic', '.heif')):
                # Get absolute path
                abs_path = os.path.join(root, file)
                if abs_path not in excluded_images:
                    image_paths.append(abs_path)
                    app.logger.debug(f"Found image: {abs_path}")
    
    if not image_paths:
        app.logger.warning(f"No images found in {IMAGE_FOLDER}")
    else:
        app.logger.info(f"Found {len(image_paths)} images")
    
    return image_paths

def initialize_image_pairs(a=False):
    global image_pairs, current_pair_index
    image_paths = get_image_paths()
    
    if not image_paths:
        app.logger.error("No images found to initialize pairs")
        return
        
    app.logger.debug(f"Initializing pairs with {len(image_paths)} images")
    
    n = len(image_paths)
    initial_pairs = []
    for i in range(n):
        pair = (image_paths[i], image_paths[(i+1) % n])
        initial_pairs.append(pair)
    
    app.logger.debug(f"Created {len(initial_pairs)} initial pairs")
    
    random.shuffle(initial_pairs)
    remaining_pairs = list(itertools.combinations(image_paths, 2))
    remaining_pairs = [pair for pair in remaining_pairs if pair not in initial_pairs]
    
    app.logger.debug(f"Created {len(remaining_pairs)} remaining pairs")
    
    image_pairs = initial_pairs + remaining_pairs
    image_pairs = [pair for pair in image_pairs if pair[0] not in excluded_images and pair[1] not in excluded_images]
    
    app.logger.info(f"Total pairs created: {len(image_pairs)}")
    
    random.shuffle(image_pairs[n:])
    current_pair_index = 0

@app.route('/')
def index():
    return render_template('index.html')

def smart_shuffle():
    """
    Reorders the image pairs based on their ELO ratings and comparison counts.

    This function removes the image pairs that have already been compared, 
    retrieves the current ELO rankings and comparison counts, and then 
    sorts the remaining image pairs based on their ELO differences and 
    comparison counts. The image pairs with the smallest ELO differences 
    and comparison counts are placed first in the list.
    """
    global image_pairs
    global current_pair_index
    
    with image_pairs_lock:
        image_pairs = image_pairs[current_pair_index:]
        current_pair_index = 0
        rankings = elo_ranking.get_rankings()
        elo_dict = {image: rating.mu for image, rating in rankings}
        count_dict = {image: elo_ranking.counts.get(image, 0) for image in elo_dict}
        
        def get_elo_difference(pair):
            return abs(elo_dict.get(pair[0], 0) - elo_dict.get(pair[1], 0)) + 0.8 * (count_dict.get(pair[0], 0) + count_dict.get(pair[1], 0))
        
        image_pairs.sort(key=get_elo_difference)
        
@app.route('/smart_shuffle')
def smart_shuffle_route():
    try:
        smart_shuffle()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@app.route('/get_images')
def get_images():
    global current_pair_index
    global last_shown_image

    with image_pairs_lock:
        if current_pair_index >= len(image_pairs):
            return jsonify({'error': 'All comparisons completed in get_images'})
        
        img1, img2 = image_pairs[current_pair_index]
        if last_shown_image is not None:
            if img1 == last_shown_image:
                img1, img2 = img2, img1
            elif img2 == last_shown_image:
                pass
        last_shown_image = img1
        current_pair_index += 1
        total_pairs = len(image_pairs)
        completed_pairs = len(elo_ranking.comparison_history)
    return jsonify({
        'image1':  img1,
        'image2':  img2,
        'progress': {
            'current': completed_pairs,
            'total': total_pairs
        }
    })

@app.route('/serve_image')
def serve_image():
    try:
        image_path = request.args.get('path')
        if not image_path:
            return jsonify({'error': 'No image path provided'}), 400
            
        # Remove any URL encoding from the path
        image_path = os.path.normpath(image_path)
        
        # If the path is relative to IMAGE_FOLDER, make it absolute
        if not os.path.isabs(image_path):
            image_path = os.path.join(IMAGE_FOLDER, os.path.basename(image_path))
        
        app.logger.debug(f"Attempting to serve image: {image_path}")
        
        # Check if the file exists
        if not os.path.exists(image_path):
            app.logger.error(f"Image not found: {image_path}")
            return jsonify({'error': 'Image not found'}), 404
            
        file_extension = os.path.splitext(image_path)[1].lower()
        if file_extension == '.webp':
            mimetype = 'image/webp'
        elif file_extension in ['.jpg', '.jpeg']:
            mimetype = 'image/jpeg'
        elif file_extension == '.png':
            mimetype = 'image/png'
        elif file_extension == '.gif':
            mimetype = 'image/gif'
        else:
            mimetype = 'image/jpeg'  # default
            
        app.logger.debug(f"Serving image with mimetype: {mimetype}")
        return send_file(image_path, mimetype=mimetype)
    except Exception as e:
        app.logger.error(f"Error serving image: {str(e)}")
        return jsonify({'error': str(e)}), 500

def autosave_rankings():
    global elo_ranking, current_directory
    
    if not current_directory:
        app.logger.warning("No image directory selected. Autosave aborted.")
        return

    # Get current date
    current_date = datetime.now().strftime("%Y-%m-%d")
    
    # Save rankings
    rankings = elo_ranking.get_rankings()
    rankings_filename = os.path.join(current_directory, f'image_rankings_autosave_{current_date}.csv')
    with open(rankings_filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Image', 'ELO', 'Uncertainty', 'Upvotes', 'Downvotes'])
        for image, rating in rankings:
            writer.writerow([
                image,
                round(rating.mu, 2),
                round(rating.sigma, 2),
                elo_ranking.upvotes.get(image, 0),
                elo_ranking.downvotes.get(image, 0)
            ])
    
    # Save comparisons
    comparisons = elo_ranking.comparison_history
    comparisons_filename = os.path.join(current_directory, f'comparisons_autosave_{current_date}.csv')
    with open(comparisons_filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Winner', 'Loser'])
        for winner, loser in comparisons:
            if winner is None:
                writer.writerow(['None', loser])
            else:
                writer.writerow([winner, loser])

    app.logger.info(f"Autosave completed. Files saved in {current_directory}: {os.path.basename(rankings_filename)}, {os.path.basename(comparisons_filename)}")

@app.route('/update_elo', methods=['POST'])
def update_elo():
    global comparisons_since_autosave
    data = request.json
    winner = data['winner']
    loser = data['loser']
    elo_ranking.update_rating((winner, loser))
    if data.get('exclude_loser', False):
        excluded_images.add(loser)
        # Recalculate image pairs
        initialize_image_pairs()
    
    # Increment the counter and check if it's time to autosave
    comparisons_since_autosave += 1
    if comparisons_since_autosave >= 10:
        autosave_rankings()
        comparisons_since_autosave = 0
    
    return jsonify({'success': True})

@app.route('/remove_image', methods=['POST'])
def remove_image():
    image = request.json['del_img']
    global image_pairs
    image_pairs = [(img1, img2) for img1, img2 in image_pairs if img1!= image and img2!= image]
    elo_ranking.remove_image(image)
    return jsonify({'success': True})

@app.route('/get_rankings')
def get_rankings():
    try:
        rankings = elo_ranking.get_rankings()
        return jsonify([
            {
                'image': image,
                'elo': rating.mu,
                'uncertainty': rating.sigma,
                'count': elo_ranking.counts.get(image, 0),
                'upvotes': elo_ranking.upvotes.get(image, 0),
                'downvotes': elo_ranking.downvotes.get(image, 0),
                'excluded': image in excluded_images
            }
            for image, rating in rankings
        ])
    except Exception as e:
        app.logger.error(f"Error in get_rankings: {str(e)}.")
        return jsonify({'error': str(e)}), 500

@app.route('/get_progress')
def get_progress():
    return jsonify({
        'current': current_pair_index,
        'total': len(image_pairs)
    })

@app.route('/set_directory', methods=['POST'])
def set_directory():
    global IMAGE_FOLDER, current_directory, elo_ranking, image_pairs, current_pair_index, comparisons_since_autosave
    
    try:
        data = request.json
        directory = data.get('directory')
        
        if not directory:
            return jsonify({'success': False, 'error': 'No directory provided'}), 400
        
        # Get the absolute path of the current working directory
        cwd = os.getcwd()
        
        # If we receive a relative path from the file input
        if not os.path.isabs(directory):
            # First try to find the directory in the current working directory
            potential_path = os.path.join(cwd, directory)
            if os.path.exists(potential_path):
                directory = potential_path
            else:
                # Try to find the directory in the parent directory
                parent_dir = os.path.dirname(cwd)
                potential_path = os.path.join(parent_dir, directory)
                if os.path.exists(potential_path):
                    directory = potential_path
        
        # Normalize the path (converts slashes to the correct format for the OS)
        directory = os.path.normpath(directory)
        
        app.logger.debug(f"Attempting to set directory to: {directory}")
        
        if not os.path.exists(directory):
            # Try to find the directory by walking up the directory tree
            current_path = cwd
            while current_path != os.path.dirname(current_path):  # Stop at root directory
                potential_path = os.path.join(current_path, directory)
                if os.path.exists(potential_path):
                    directory = potential_path
                    break
                current_path = os.path.dirname(current_path)
        
        if not os.path.exists(directory):
            app.logger.error(f"Directory does not exist: {directory}")
            return jsonify({'success': False, 'error': f'Directory does not exist: {directory}'}), 400
            
        if not os.path.isdir(directory):
            app.logger.error(f"Not a directory: {directory}")
            return jsonify({'success': False, 'error': f'Not a directory: {directory}'}), 400
            
        # Update directory paths
        IMAGE_FOLDER = directory
        current_directory = directory
        
        app.logger.info(f"Successfully set directory to: {directory}")
        
        # Reset the ranking system
        elo_ranking = TrueSkillRanking()
        initialize_image_pairs()
        current_pair_index = 0
        comparisons_since_autosave = 0
        
        return jsonify({'success': True, 'directory': directory})
    except Exception as e:
        app.logger.error(f"Error in set_directory: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/export_rankings')
def export_rankings():
    app.logger.info("Export rankings route called.")
    try:
        rankings = elo_ranking.get_rankings()
        app.logger.info(f"Rankings: {rankings}")
        if not rankings:
            app.logger.warning("No rankings data available.")
            return jsonify({'error': 'No rankings data available. Please make some comparisons first.'}), 400

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Image', 'ELO', 'Uncertainty', 'Upvotes', 'Downvotes'])
        for image, rating in rankings:
            writer.writerow([
                image,
                round(rating.mu, 2),
                round(rating.sigma, 2),
                elo_ranking.upvotes.get(image, 0),
                elo_ranking.downvotes.get(image, 0)
            ])
        
        output.seek(0)
        app.logger.info("CSV data created successfully.")
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={"Content-disposition": "attachment; filename=image_rankings.csv"}
        )
    except Exception as e:
        app.logger.error(f"Error in export_rankings: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/export_comparisons')
def export_comparisons():
    app.logger.info("Export comparisons route called.")
    try:
        comparisons = elo_ranking.comparison_history
        app.logger.info(f"Comparisons: {comparisons}")
        if not comparisons:
            app.logger.warning("No comparisons data available.")
            return jsonify({'error': 'No comparisons data available. Please make some comparisons first.'}), 400

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Winner', 'Loser'])
        for winner, loser in comparisons:
            if winner is None:
                writer.writerow(['None', loser])
            else:
                writer.writerow([winner, loser])
        
        output.seek(0)
        app.logger.info("CSV data created successfully.")
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={"Content-disposition": "attachment; filename=comparisons.csv"}
        )
    except Exception as e:
        app.logger.error(f"Error in export_comparisons: {str(e)}.")
        return jsonify({'error': str(e)}), 500

@app.route('/import_comparison_history', methods=['POST'])
def import_comparison_history():
    global image_pairs
    file = request.files['file']
    append = request.form.get('append', 'false') == 'true'

    reader = csv.reader(file.read().decode('utf-8').splitlines())
    next(reader)  # Skip header row

    if not append:
        elo_ranking.comparison_history = []
        elo_ranking.recalculate_rankings()

    pairs_to_add = set()
    losers_to_remove = set()
    pairs_to_remove = set()
    for row in reader:
        winner, loser = row
        if winner == 'None':  # Handle cases where winner is None
            losers_to_remove.add(loser)
        else:
            pairs_to_add.add((winner, loser))
        # Collect pairs to remove
        pairs_to_remove.add((winner, loser))
        pairs_to_remove.add((loser, winner))

    # Remove losers from image_pairs and elo_ranking
    image_pairs = [(img1, img2) for img1, img2 in image_pairs if img1 not in losers_to_remove and img2 not in losers_to_remove]
    elo_ranking.update_rating(pairs_to_add)
    elo_ranking.remove_image(losers_to_remove)
    
    # Remove duplicate pairs from image_pairs
    image_pairs = [(img1, img2) for img1, img2 in image_pairs if (img1, img2) not in pairs_to_remove]

    return jsonify({'success': True})

@app.route('/exclude_image', methods=['POST'])
def exclude_image():
    global excluded_images
    data = request.json
    excluded_image = data['excluded_image']
    excluded_images.add(excluded_image)
    # Recalculate image pairs
    initialize_image_pairs()
    return jsonify({'success': True})

@app.route('/clear_excluded_images', methods=['POST'])
def clear_excluded_images():
    global excluded_images
    excluded_images.clear()
    # Recalculate image pairs
    initialize_image_pairs()
    return jsonify({'success': True})

# Add a new route to get the current directory
@app.route('/get_current_directory')
def get_current_directory():
    global current_directory
    return jsonify({'directory': current_directory if current_directory else None})

@app.route('/health')
def health():
    """Simple health check endpoint"""
    return jsonify({'status': 'ok', 'platform': platform.system()})

@app.route('/select_directory_dialog', methods=['GET'])
def select_directory_dialog():
    """
    Opens a native directory picker dialog on the server side using AppleScript.
    This is necessary on macOS because browsers don't provide real paths from file inputs.
    """
    app.logger.info("select_directory_dialog endpoint called")
    try:
        if platform.system() != 'Darwin':
            app.logger.warning("select_directory_dialog called on non-macOS system")
            return jsonify({'success': False, 'error': 'This endpoint is only available on macOS'}), 400
        
        # Use AppleScript to open a native directory picker
        applescript = '''
        tell application "System Events"
            activate
        end tell
        set theFolder to choose folder with prompt "Select Image Directory"
        return POSIX path of theFolder
        '''
        
        app.logger.debug("Running AppleScript to open directory picker")
        try:
            # Run the AppleScript command
            result = subprocess.run(
                ['osascript', '-e', applescript],
                capture_output=True,
                text=True,
                timeout=120  # Increased timeout to 2 minutes
            )
            
            app.logger.debug(f"AppleScript return code: {result.returncode}")
            app.logger.debug(f"AppleScript stdout: {result.stdout}")
            app.logger.debug(f"AppleScript stderr: {result.stderr}")
            
            if result.returncode != 0:
                # User cancelled or error occurred
                error_msg = result.stderr.strip() if result.stderr else 'Unknown error'
                if 'User canceled' in error_msg or 'canceled' in error_msg.lower():
                    app.logger.info("User cancelled directory selection")
                    return jsonify({'success': False, 'error': 'Directory selection cancelled'}), 400
                else:
                    app.logger.error(f"AppleScript error: {error_msg}")
                    return jsonify({'success': False, 'error': f'Error opening directory dialog: {error_msg}'}), 500
            
            directory = result.stdout.strip()
            
            if not directory:
                app.logger.warning("AppleScript returned empty directory")
                return jsonify({'success': False, 'error': 'No directory selected'}), 400
            
            app.logger.info(f"Directory selected via AppleScript: {directory}")
            
        except subprocess.TimeoutExpired:
            app.logger.error("Directory selection timed out")
            return jsonify({'success': False, 'error': 'Directory selection timed out'}), 400
        except Exception as e:
            app.logger.error(f"Error running AppleScript: {str(e)}")
            return jsonify({'success': False, 'error': f'Error opening directory dialog: {str(e)}'}), 500
        
        # Normalize the path
        directory = os.path.normpath(directory)
        
        if not os.path.exists(directory):
            app.logger.error(f"Selected directory does not exist: {directory}")
            return jsonify({'success': False, 'error': f'Directory does not exist: {directory}'}), 400
        
        if not os.path.isdir(directory):
            app.logger.error(f"Selected path is not a directory: {directory}")
            return jsonify({'success': False, 'error': f'Not a directory: {directory}'}), 400
        
        # Now set the directory using the same logic as set_directory
        global IMAGE_FOLDER, current_directory, elo_ranking, image_pairs, current_pair_index, comparisons_since_autosave
        
        IMAGE_FOLDER = directory
        current_directory = directory
        
        app.logger.info(f"Successfully set directory to: {directory}")
        
        # Reset the ranking system
        elo_ranking = TrueSkillRanking()
        initialize_image_pairs()
        current_pair_index = 0
        comparisons_since_autosave = 0
        
        return jsonify({'success': True, 'directory': directory})
        
    except Exception as e:
        app.logger.error(f"Error in select_directory_dialog: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    initialize_image_pairs()
    app.run(debug=False, threaded=True)