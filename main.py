import time
from log_parser import process_files


LOG_FOLDER = 'logs'   # Folder containing DL log files


def main():
    print('DL Failure Monitor Started...')

    while True:
        try:
            results = process_files(LOG_FOLDER)

            for message in results:
                print(message)

        except Exception as e:
            print('Error:', e)

        # Check every 10 seconds
        time.sleep(10)


if __name__ == '__main__':
    main()
