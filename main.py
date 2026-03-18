from __future__ import annotations

import logging

import config
from pcloud_client import parse_permission_level
from storage import Profile, ProfileStorage, format_profiles_table
from workflow import WorkflowSummary, prepare_profile, run_pcloud_workflow


def setup_logging() -> logging.Logger:
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("pcloud_ads")


def print_header(storage: ProfileStorage) -> None:
    profile_count = len(storage.list_profiles())
    print("=" * 68)
    print(" ADS BROWSER + PCLOUD REQUEST WORKFLOW")
    print("=" * 68)
    print(f" Profiles in database: {profile_count}")
    print(f" Name file:   {config.FOLDER_NAME_FILE}")
    print(f" Message file:{config.MESSAGE_FILE}")
    print(f" Email file:  {config.EMAILS_FILE}")
    print("-" * 68)
    print("1. Add ADS profile")
    print("2. Show profiles")
    print("3. Check pCloud session for profile")
    print("4. Run folder create + share workflow")
    print("5. Delete profile")
    print("6. Exit")
    print("=" * 68)


def prompt(message: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{message}{suffix}: ").strip()
    if value:
        return value
    if default is not None:
        return default
    return ""


def prompt_int(message: str, default: int) -> int:
    raw_value = prompt(message, str(default))
    return int(raw_value)


def add_profile(storage: ProfileStorage) -> None:
    print("\nAdd ADS Browser profile")
    title = prompt("Local profile title")
    ads_profile_id = prompt("ADS profile id")
    start_url = prompt("Start URL", config.DEFAULT_START_URL)
    parent_folder_id = prompt_int("Parent folder id", config.DEFAULT_PARENT_FOLDER_ID)
    permission_input = prompt("Permission (view/edit/manage or number)", str(config.DEFAULT_SHARE_PERMISSION))
    permission_level = parse_permission_level(permission_input)

    profile = storage.add_profile(
        title=title,
        ads_profile_id=ads_profile_id,
        start_url=start_url,
        parent_folder_id=parent_folder_id,
        permission_level=permission_level,
    )
    print(f"\nSaved profile #{profile.id}: {profile.title}")


def show_profiles(storage: ProfileStorage) -> None:
    print()
    print(format_profiles_table(storage.list_profiles()))


def choose_profile(storage: ProfileStorage) -> Profile:
    profiles = storage.list_profiles()
    if not profiles:
        raise ValueError("No profiles found. Add a profile first.")

    print()
    print(format_profiles_table(profiles))
    selected_id = prompt_int("Enter profile id", profiles[0].id)
    return storage.get_profile(selected_id)


def check_profile(storage: ProfileStorage, logger: logging.Logger) -> None:
    profile = choose_profile(storage)
    prepared = prepare_profile(profile, logger)
    userinfo = prepared.userinfo
    print("\nSession check successful")
    print(f" Profile:     {profile.title}")
    print(f" ADS id:      {profile.ads_profile_id}")
    print(f" pCloud mail: {userinfo.get('email', '')}")
    print(f" Location ID: {prepared.auth.location_id}")
    print(f" Current URL: {prepared.auth.current_url}")


def print_summary(summary: WorkflowSummary) -> None:
    print("\nWorkflow finished")
    print("-" * 68)
    print(f" Profile:          {summary.profile_title}")
    print(f" pCloud account:   {summary.account_email}")
    print(f" Folder:           {summary.folder_name} (id={summary.folder_id})")
    print(f" Total emails:     {summary.total_emails}")
    print(f" Success:          {summary.success_count}")
    print(f" Skipped:          {summary.skipped_count}")
    print(f" Failed:           {summary.failed_count}")
    print("-" * 68)

    failed_rows = [attempt for attempt in summary.attempts if attempt.status == "failed"]
    if failed_rows:
        print("Failed emails:")
        for attempt in failed_rows:
            print(f" - {attempt.email}: {attempt.detail} (code={attempt.result_code})")


def delete_profile(storage: ProfileStorage) -> None:
    profile = choose_profile(storage)
    confirmation = prompt(f"Delete profile '{profile.title}'? type YES", "NO")
    if confirmation != "YES":
        print("Deletion cancelled.")
        return

    if storage.delete_profile(profile.id):
        print(f"Profile #{profile.id} deleted.")
        return
    print("Profile was not deleted.")


def ensure_input_files_exist() -> None:
    required_paths = [
        config.FOLDER_NAME_FILE,
        config.MESSAGE_FILE,
        config.EMAILS_FILE,
    ]
    for path in required_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")


def pause() -> None:
    input("\nPress Enter to continue...")


def main() -> None:
    ensure_input_files_exist()
    logger = setup_logging()
    storage = ProfileStorage()

    while True:
        try:
            print()
            print_header(storage)
            action = input("Choose action: ").strip()

            if action == "1":
                add_profile(storage)
                pause()
            elif action == "2":
                show_profiles(storage)
                pause()
            elif action == "3":
                check_profile(storage, logger)
                pause()
            elif action == "4":
                profile = choose_profile(storage)
                summary = run_pcloud_workflow(profile, logger)
                print_summary(summary)
                pause()
            elif action == "5":
                delete_profile(storage)
                pause()
            elif action == "6":
                print("Exit.")
                break
            else:
                print("Unknown action.")
                pause()
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except Exception as exc:
            logger.exception("Action failed")
            print(f"\nError: {exc}")
            pause()


if __name__ == "__main__":
    main()
