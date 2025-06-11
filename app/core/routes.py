"""
Main application routes for web interface
"""
from flask import Blueprint, render_template, redirect, url_for, request, Response, current_app, session, flash
import threading
import logging
import os
import cv2
import numpy as np

from app.services.video_service import VideoService
from app.models.detection_model import DetectionModel
from app.core.firebase_client import fetch_camera_settings, save_camera_settings
from app import socketio

# Configure logging
logger = logging.getLogger(__name__)

# Create blueprint
main_bp = Blueprint('main', __name__)

# Shared resources
detection_model = None
video_service = None

def initialize_services():
    """Initialize shared services before first request."""
    global detection_model, video_service
    
    # Get settings from database
    settings = fetch_camera_settings()
    
    # Initialize detection model if not already done
    if detection_model is None:
        # Create config with settings
        config = current_app.config.copy()
        # Add GPU preference from settings if available
        if settings and 'use_gpu' in settings:
            config['USE_GPU'] = settings['use_gpu']
            
        detection_model = DetectionModel(config)
        logger.info("Detection model initialized")
    
    # Initialize video service if not already done
    if video_service is None:
        # Get camera settings
        video_source = settings.get('video_source', 'camera')
        if video_source == 'demo':
            video_path = 'app/static/videos/demo.mp4'
        else:
            video_path = settings.get('camera_url', current_app.config['VIDEO_PATH'])
            
        frame_rate = int(settings.get('frame_rate', current_app.config['FRAME_RATE']))
        
        # Parse resolution
        resolution_str = settings.get('resolution', None)
        if resolution_str and isinstance(resolution_str, str) and ',' in resolution_str:
            width, height = map(int, resolution_str.split(','))
            resolution = (width, height)
        else:
            resolution = current_app.config['RESOLUTION']
        
        # Create video service
        video_service = VideoService(detection_model, socketio, 
                                     video_path, frame_rate, resolution)
        
        # Start capture thread
        video_service.start_capture_thread()
        logger.info("Video service initialized and started")
        
        # Set door area if configured
        if settings and 'door_area' in settings:
            door_area = settings['door_area']
            inside_direction = settings.get('inside_direction', 'right')
            
            try:
                x1 = door_area.get('x1')
                y1 = door_area.get('y1')
                x2 = door_area.get('x2')
                y2 = door_area.get('y2')
                detection_model.set_door_area(x1, y1, x2, y2)
                detection_model.set_inside_direction(inside_direction)
                logger.info(f"Door area set to: {(x1, y1, x2, y2)}, inside: {inside_direction}")
            except Exception as e:
                logger.error(f"Error setting door area: {e}")

# Register initialization function to run before first request
@main_bp.before_app_request
def initialize_before_request():
    """Initialize services if not already initialized."""
    if detection_model is None or video_service is None:
        initialize_services()

@main_bp.route('/')
def index():
    """Landing page route."""
    return render_template('login.html')

@main_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == 'admin' and password == 'admin123':
            # Set session variables to mark user as logged in
            session['logged_in'] = True
            return redirect(url_for('main.home'))
        else:
            flash('Invalid username or password')
    return render_template('login.html')

@main_bp.route('/home')
def home():
    """Home page with video feed."""
    global detection_model, video_service
    
    # Get current counts
    entries = 0
    exits = 0
    people_in_room = 0
    
    if detection_model:
        entries, exits = detection_model.get_entry_exit_count()
        people_in_room = max(0, entries - exits)  # Calculate people in room from entries/exits
    
    # Get door configuration
    door_defined = False
    door_coordinates = None
    inside_direction = 'right'
    
    if detection_model:
        door_area = detection_model.get_door_area()
        if door_area and all(v is not None for v in door_area):
            door_defined = True
            door_coordinates = {
                'x1': door_area[0],
                'y1': door_area[1],
                'x2': door_area[2],
                'y2': door_area[3]
            }
            inside_direction = detection_model.get_inside_direction()
    
    # Get video source details
    video_source = "Camera"
    resolution = "640 x 480"
    frame_rate = 30
    
    if video_service:
        # Determine video source type
        if video_service.is_file:
            video_source = f"File: {os.path.basename(video_service.video_path)}"
        elif video_service.is_rtsp:
            video_source = f"RTSP Stream"
        elif video_service.is_camera:
            video_source = f"Camera #{video_service.video_path}"
        
        # Get resolution and frame rate
        resolution = f"{video_service.resolution[0]} x {video_service.resolution[1]}"
        frame_rate = video_service.frame_rate
    
    return render_template('home.html',
                          people_in_room=people_in_room,
                          entries=entries,
                          exits=exits,
                          door_defined=door_defined,
                          door_coordinates=door_coordinates,
                          inside_direction=inside_direction,
                          video_source=video_source,
                          resolution=resolution,
                          frame_rate=frame_rate)

@main_bp.route('/video_feed')
def video_feed():
    """Video streaming route."""
    global video_service
    
    # Check if services are initialized
    if video_service is None:
        initialize_services()
        
    return Response(video_service.generate_frames(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

@main_bp.route('/raw_video_feed')
def raw_video_feed():
    """Raw video streaming route without detection for camera settings."""
    global video_service
    
    # Check if services are initialized
    if video_service is None:
        initialize_services()
        
    return Response(video_service.generate_raw_frames(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

@main_bp.route('/dashboard')
def dashboard():
    """Dashboard with video feed."""
    # Redirect to home since they show the same content
    return redirect(url_for('main.home'))

@main_bp.route('/camera-settings', methods=['GET', 'POST'])
def camera_settings():
    """Camera settings page."""
    global video_service
    
    # Check if services are initialized
    if video_service is None:
        initialize_services()
    
    if request.method == 'POST':
        try:
            # Get video source setting
            video_source = request.form.get('video_source', 'camera')
            
            if video_source == 'demo':
                # Use demo video with default settings
                video_path = 'app/static/videos/demo.mp4'
                # Use default or existing settings for frame rate and resolution
                settings = fetch_camera_settings()
                frame_rate = int(settings.get('frame_rate', 30))
                resolution_str = settings.get('resolution', '640,480')
            else:
                # Use camera settings from form
                camera_url = request.form.get('camera_url', '0')
                frame_rate = int(request.form.get('frame_rate', 30))
                resolution_str = request.form.get('resolution', '640,480')
                video_path = camera_url
            
            # Parse resolution
            if ',' in resolution_str:
                width, height = map(int, resolution_str.split(','))
                resolution = (width, height)
            else:
                resolution = current_app.config['RESOLUTION']

            # Update video service
            if video_service:
                video_service.update_settings(video_path, frame_rate, resolution)
                
            # Prepare settings to save
            save_data = {
                'video_source': video_source,
                'frame_rate': frame_rate,
                'resolution': resolution_str
            }
            
            # Add camera-specific settings
            if video_source == 'camera':
                save_data['camera_url'] = camera_url
            else:  # demo mode
                save_data['camera_url'] = video_path  # Save the demo video path
                
            # Save all settings
            save_camera_settings(**save_data)
            
            logger.info(f"Saved camera settings: {save_data}")
            logger.info(f"Camera settings updated: source={video_source}, path={video_path}")
            
            # If AJAX request, return JSON response
            if request.headers.get('Accept') == 'application/json':
                return {'success': True, 'message': 'Camera settings updated successfully'}
            
            # For regular form submission, redirect
            return redirect(url_for('main.camera_settings'))
            
        except Exception as e:
            logger.error(f"Error updating camera settings: {str(e)}")
            
            # If it's an OpenCV error
            if 'cv2.error' in str(e) or 'OpenCV' in str(e):
                error_message = "OpenCV error: Cannot access camera with these settings. Please check camera URL and resolution."
            else:
                error_message = f"Error updating camera settings: {str(e)}"
            
            # If AJAX request, return JSON with error
            if request.headers.get('Accept') == 'application/json':
                return {'success': False, 'error': error_message}, 400
            
            # For regular form submission, flash error and return to form
            flash(error_message, 'error')
            
    # Get current settings
    settings = fetch_camera_settings()
    if settings is None:
        settings = {}
    
    # Ensure video_source is set in settings
    if 'video_source' not in settings:
        settings['video_source'] = 'camera'  # default value
    
    # Get door settings
    door_area = None
    inside_direction = 'right'
    
    if detection_model and detection_model.door_defined and detection_model.door_area:
        x1, y1, x2, y2 = detection_model.door_area
        door_area = {'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2}
        inside_direction = detection_model.inside_direction
    elif settings and 'door_area' in settings:
        door_area = settings['door_area']
        inside_direction = settings.get('inside_direction', 'right')
        
    return render_template('camera-settings.html', 
                          settings=settings,
                          door_area=door_area,
                          inside_direction=inside_direction,
                          cuda_available=detection_model.cuda_available)
        
    return render_template('camera-settings.html', 
                          settings=settings,
                          door_area=door_area,
                          inside_direction=inside_direction,
                          cuda_available=detection_model.cuda_available)

@main_bp.route('/reports')
def reports():
    """Reports page."""
    from app.core.firebase_client import get_people_count_logs
    logs = get_people_count_logs(limit=100)
    
    # Calculate totals from logs
    total_entries = 0
    total_exits = 0
    
    for log in logs:
        # Handle both 'entries' and 'people_entered' field names
        entries = log.get('entries', 0) or log.get('people_entered', 0) or 0
        exits = log.get('exits', 0) or log.get('people_exited', 0) or 0
        
        total_entries += entries
        total_exits += exits
    
    return render_template('reports.html', 
                         logs=logs, 
                         total_entries=total_entries, 
                         total_exits=total_exits)

@main_bp.route('/alerts')
def alerts():
    """System alerts page."""
    return render_template('alerts.html')

@main_bp.route('/toggle-processing-device', methods=['POST'])
def toggle_processing_device():
    """Toggle between CPU and GPU processing."""
    global detection_model
    
    if not detection_model:
        return {'success': False, 'error': 'Detection model not initialized'}
    
    # Get the use_gpu value from the request
    use_gpu = request.json.get('use_gpu', True)
    
    # Update the model's processing device
    result = detection_model.set_processing_device(use_gpu)
    
    # Save the setting to the database
    save_camera_settings(use_gpu=use_gpu)
    
    # Return device information
    return result

@main_bp.route('/debug/rtsp_test')
def rtsp_test():
    """Debug endpoint to test RTSP connection and display diagnostics."""
    global video_service
    
    # Check if services are initialized
    if video_service is None:
        initialize_services()
    
    # Get current settings
    settings = fetch_camera_settings()
    
    diagnostics = {
        'rtsp_url': settings.get('camera_url', 'Not configured'),
        'video_service_initialized': video_service is not None,
        'capture_opened': video_service.cap.isOpened() if video_service and video_service.cap else False,
        'is_rtsp': video_service.is_rtsp if video_service else False,
        'health_info': video_service.check_connection_health() if video_service else None,
        'source_info': video_service.capture_manager.get_source_info() if video_service else None,
        'opencv_version': cv2.__version__,
        'opencv_backends': []
    }
    
    # Test OpenCV backends availability
    backends = [
        ('FFMPEG', cv2.CAP_FFMPEG),
        ('GSTREAMER', cv2.CAP_GSTREAMER),
        ('DSHOW', cv2.CAP_DSHOW)
    ]
    
    for name, backend in backends:
        try:
            test_cap = cv2.VideoCapture()
            test_cap.open('', backend)
            diagnostics['opencv_backends'].append(f"{name}: Available")
            test_cap.release()
        except:
            diagnostics['opencv_backends'].append(f"{name}: Not available")
    
    return render_template('debug_rtsp.html', diagnostics=diagnostics)

@main_bp.route('/debug/test_pattern')
def debug_test_pattern():
    """Generate a test pattern image for debugging."""
    global video_service
    
    if video_service is None:
        initialize_services()
    
    # Create test pattern
    test_frame = video_service.frame_processor.create_test_pattern_frame()
    
    # Convert to JPEG
    ret, buffer = cv2.imencode('.jpg', test_frame)
    if not ret:
        # Create a simple error frame if encoding fails
        error_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(error_frame, "Test Pattern Generation Failed", (100, 240), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        ret, buffer = cv2.imencode('.jpg', error_frame)
    
    return Response(buffer.tobytes(), mimetype='image/jpeg')