import os
import shutil
import time
from pathlib import Path
from typing import Optional

import ffmpeg

from utils.logger_manager import logger


class VideoManagement:
    """Handles video file operations and conversions."""
    
    DEFAULT_WAIT_TIMEOUT = 10
    WAIT_INTERVAL = 0.5
    RAW_FLV_FOLDER = "raw_flv"  # Folder to store original FLV files
    
    @staticmethod
    def wait_for_file_release(file: str, timeout: int = DEFAULT_WAIT_TIMEOUT) -> bool:
        """
        Wait until the file is released (not locked anymore) or timeout is reached.
        
        Args:
            file: Path to the file
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if file is released, False if timeout reached
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                with open(file, "ab"):
                    return True
            except PermissionError:
                time.sleep(VideoManagement.WAIT_INTERVAL)
        return False

    @staticmethod
    def get_file_size_mb(file: str) -> float:
        """Get file size in megabytes."""
        try:
            return Path(file).stat().st_size / (1024 * 1024)
        except OSError:
            return 0.0

    @staticmethod
    def _move_to_raw_flv(file: str) -> Optional[str]:
        """
        Move FLV file to raw_flv folder instead of deleting.
        
        Args:
            file: Path to the FLV file
            
        Returns:
            New path if moved successfully, None otherwise
        """
        try:
            file_path = Path(file)
            # Create raw_flv folder in the same directory as the video
            raw_flv_dir = file_path.parent / VideoManagement.RAW_FLV_FOLDER
            raw_flv_dir.mkdir(exist_ok=True)
            
            # Move file to raw_flv folder
            new_path = raw_flv_dir / file_path.name
            shutil.move(str(file), str(new_path))
            logger.debug(f"Moved original FLV to: {new_path}")
            return str(new_path)
        except Exception as e:
            logger.warning(f"Failed to move FLV to raw_flv folder: {e}")
            return None

    @staticmethod
    def convert_flv_to_mp4(file: str) -> Optional[str]:
        """
        Convert the video from FLV or TS format to MP4 format.
        Fixes audio/video sync issues by re-encoding with proper timestamp handling.
        
        Args:
            file: Path to the input file
            
        Returns:
            Path to the converted MP4 file, or None if conversion failed
        """
        if file.endswith("_flv.mp4"):
            output_file = file.replace("_flv.mp4", ".mp4")
        elif file.endswith("_hls.ts"):
            output_file = file.replace("_hls.ts", ".mp4")
        elif file.endswith(".ts"):
            output_file = file.replace(".ts", ".mp4")
        else:
            # Generic fallback: strip extension and append .mp4
            file_path = Path(file)
            output_file = str(file_path.with_suffix(".mp4"))
            
        # Ensure we don't overwrite input
        if output_file == file:
            output_file = str(Path(file).with_stem(Path(file).stem + "_converted").with_suffix(".mp4"))

        file_size = VideoManagement.get_file_size_mb(file)
        
        logger.info(f"Converting {file} to MP4 format... ({file_size:.1f} MB)")

        if not VideoManagement.wait_for_file_release(file):
            logger.error(f"File {file} is still locked after waiting. Skipping conversion.")
            return None

        try:
            # Use timestamp correction flags to fix A/V sync issues:
            # -fflags +genpts+igndts: Generate new PTS and ignore corrupted DTS
            # -async 1: Sync audio to timestamps  
            # -vsync cfr: Constant frame rate for video
            ffmpeg.input(file, fflags='+genpts+igndts').output(
                output_file,
                acodec='aac',           # Re-encode audio to fix sync
                vcodec='copy',          # Copy video (fast, keeps quality)
                audio_bitrate='128k',
                af='aresample=async=1000',  # Resample audio to fix drift
                y='-y',
            ).run(quiet=True)
            
            # Move the original FLV file to raw_flv folder
            VideoManagement._move_to_raw_flv(file)
            
            output_size = VideoManagement.get_file_size_mb(output_file)
            logger.info(f"Finished converting: {output_file} ({output_size:.1f} MB)\n")
            
            return output_file
            
        except ffmpeg.Error as e:
            error_msg = e.stderr.decode() if hasattr(e, 'stderr') and e.stderr else str(e)
            logger.warning(f"Audio re-encode failed, trying fallback method: {error_msg}")
            
            # Fallback: try simpler conversion with just timestamp fix
            try:
                ffmpeg.input(file, fflags='+genpts+igndts+discardcorrupt').output(
                    output_file,
                    c='copy',
                    movflags='+faststart',
                    y='-y',
                ).run(quiet=True)
                
                VideoManagement._move_to_raw_flv(file)
                output_size = VideoManagement.get_file_size_mb(output_file)
                logger.info(f"Fallback conversion done: {output_file} ({output_size:.1f} MB)\n")
                return output_file
            except ffmpeg.Error as e2:
                error_msg2 = e2.stderr.decode() if hasattr(e2, 'stderr') and e2.stderr else str(e2)
                logger.error(f"Fallback conversion also failed: {error_msg2}")
                return None
                
        except OSError as e:
            logger.error(f"File operation error: {e}")
            return None
