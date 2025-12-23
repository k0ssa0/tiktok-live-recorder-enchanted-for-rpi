import sys
import os
import multiprocessing
import atexit
import signal

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def setup_signal_handlers():
    """
    Set up signal handlers to prevent unexpected shutdowns.
    Ignores SIGHUP (terminal hangup) so the app survives SSH disconnections.
    """
    def signal_handler(signum, frame):
        sig_name = signal.Signals(signum).name
        # Import logger here to avoid circular imports
        try:
            from utils.logger_manager import logger
            logger.warning(f"Received signal {sig_name} ({signum}) - ignoring to keep running")
        except:
            print(f"[WARNING] Received signal {sig_name} ({signum}) - ignoring")
    
    # Ignore SIGHUP (sent when terminal closes/SSH disconnects)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal_handler)
    
    # Handle SIGPIPE gracefully (broken pipe)
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def record_user(
    user, url, room_id, mode, interval, proxy, output, duration, use_telegram, cookies
):
    from core.tiktok_recorder import TikTokRecorder
    from utils.logger_manager import logger
    
    try:
        TikTokRecorder(
            url=url,
            user=user,
            room_id=room_id,
            mode=mode,
            automatic_interval=interval,
            cookies=cookies,
            proxy=proxy,
            output=output,
            duration=duration,
            use_telegram=use_telegram,
        ).run()
    except KeyboardInterrupt:
        logger.info("Recording interrupted by user.")
    except Exception as e:
        logger.error(f"{e}")


def run_recordings(args, mode, cookies):
    from utils.logger_manager import logger
    
    if isinstance(args.user, list):
        processes = []
        for user in args.user:
            p = multiprocessing.Process(
                target=record_user,
                args=(
                    user,
                    args.url,
                    args.room_id,
                    mode,
                    args.automatic_interval,
                    args.proxy,
                    args.output,
                    args.duration,
                    args.telegram,
                    cookies,
                ),
            )
            p.start()
            processes.append(p)
        try:
            # Wait for all processes, checking periodically
            while any(p.is_alive() for p in processes):
                for p in processes:
                    p.join(timeout=1)
        except KeyboardInterrupt:
            logger.info("Ctrl-C detected. Waiting for processes to finish gracefully...")
            # Give processes time to finish gracefully
            for p in processes:
                p.join(timeout=10)
            # Force terminate any remaining
            for p in processes:
                if p.is_alive():
                    logger.warning(f"Force terminating process {p.pid}")
                    p.terminate()
                    p.join(timeout=5)
    else:
        record_user(
            args.user,
            args.url,
            args.room_id,
            mode,
            args.automatic_interval,
            args.proxy,
            args.output,
            args.duration,
            args.telegram,
            cookies,
        )


def main():
    from utils.args_handler import validate_and_parse_args
    from utils.utils import read_cookies
    from utils.logger_manager import logger, LoggerManager
    from utils.custom_exceptions import TikTokRecorderError
    from utils.session_manager import session_manager
    from check_updates import check_updates
    from pathlib import Path
    import shutil
    
    try:
        # Check for existing session before anything else
        action = session_manager.prompt_reconnect()
        
        if action == 'y':
            # User wants to reconnect to existing session
            session_manager.view_session_output()
            return
        elif action == 'n':
            # User wants to kill existing and start new
            if not session_manager.kill_existing_session():
                print("[!] Failed to stop existing session. Exiting.")
                return
            print("\n[*] Starting new session...\n")
        elif action == 'q':
            print("Exiting.")
            return
        # action == 'new' means no existing session, continue normally
        
        # validate and parse command line arguments
        args, mode = validate_and_parse_args()

        # Clear logs if requested
        if args.clear_logs:
            log_dir = Path.home() / "tiktok-recorder-logs"
            if log_dir.exists():
                log_count = len(list(log_dir.glob("*.log*")))
                shutil.rmtree(log_dir)
                log_dir.mkdir(exist_ok=True)
                print(f"[*] Cleared {log_count} log file(s) from {log_dir}")
            else:
                print(f"[*] No logs to clear (directory doesn't exist)")

        # Sort users alphabetically if multiple users provided
        if isinstance(args.user, list):
            args.user = sorted(args.user, key=str.lower)

        # Enable verbose mode if requested
        log_file = None
        if args.verbose:
            LoggerManager.enable_verbose(True)
            logger.debug("Verbose mode initialized")
            logger.debug(f"Arguments: {args}")
            logger.debug(f"Mode: {mode}")
            
            # Get the log file path from logger
            if LoggerManager._file_handler:
                log_file = LoggerManager._file_handler.baseFilename

        # check for updates
        if args.update_check is True:
            logger.info("Checking for updates...\n")
            if check_updates():
                exit()
        else:
            logger.info("Skipped update check\n")

        # read cookies from the config file
        cookies = read_cookies()
        if args.verbose:
            logger.debug(f"Cookies loaded: {len(cookies)} entries")

        # Start session tracking with log file path
        user_for_session = args.user[0] if isinstance(args.user, list) else args.user
        session_manager.start_session(user_for_session or "unknown", log_file=log_file)
        
        # Register cleanup on exit
        atexit.register(session_manager.end_session)

        # run the recordings based on the parsed arguments
        run_recordings(args, mode, cookies)

    except KeyboardInterrupt:
        logger.info("Application interrupted by user.")
        
    except TikTokRecorderError as ex:
        logger.error(f"Application Error: {ex}")

    except Exception as ex:
        logger.critical(f"Generic Error: {ex}")
    
    finally:
        session_manager.end_session()
        logger.info("Application shutdown complete.")


if __name__ == "__main__":
    # print the banner
    from utils.utils import banner

    banner()

    # check and install dependencies
    from utils.dependencies import check_and_install_dependencies

    check_and_install_dependencies()

    # set up signal handling to survive terminal disconnections
    setup_signal_handlers()

    # set up multiprocessing support
    multiprocessing.freeze_support()

    # run
    main()
