from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
import time

from dateutil import parser
from django.db.models import Q

from apps.git_data.models import (
    User,
    UserRelationship,
    AccountType,
    DiscoveryMethod,
    UserStatus,
    RelationshipType,
    Tag,
)
from apps.task_queue.tasks.utils.jobs import is_cancelled

from .enums import Action


def parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parser.parse(value)
    except Exception:
        return None


def _pages_from_search(client, query: str, logger) -> List[Dict]:
    items: List[Dict] = []
    page = 1

    while True:
        try:
            resp = client.search_users(query, per_page=100, page=page)
            page_items = resp.get("items") or []
            if not page_items:
                break

            items.extend(page_items)

            if len(page_items) < 100:
                break

            page += 1
            time.sleep(0.1)

        except Exception as exc:
            logger.error(f"Error fetching search page {page}: {exc}")
            break

    return items


def _fetch_full_user(client, username: str, logger) -> Optional[Dict]:
    try:
        data = client.get_user(username)
        time.sleep(0.1)
        return data
    except Exception as exc:
        logger.error(f"Error fetching user data for {username}: {exc}")
        return None


def _build_date_query(search_query: str, start, end) -> str:
    return f"{search_query} created:{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}"


def _placeholder_source_user_id(username: str) -> int:
    return -abs(hash(username) % 1000000)


def _build_user_fields(
    full_user_data: Dict,
    *,
    discovery_method: DiscoveryMethod,
    status: str,
) -> Dict:
    username = full_user_data["login"]

    html_url = full_user_data.get("html_url") or f"https://github.com/{username}"
    if "github.com/apps/" in html_url:
        account_type = AccountType.BOT
    elif full_user_data.get("type") == "Organization":
        account_type = AccountType.ORGANIZATION
    else:
        account_type = AccountType.USER

    return {
        "username": username,
        "source_user_id": full_user_data.get("id"),
        "name": full_user_data.get("name") or "",
        "email": full_user_data.get("email") or "",
        "location": full_user_data.get("location") or "",
        "company": full_user_data.get("company") or "",
        "bio": full_user_data.get("bio") or "",
        "url": html_url,
        "account_type": account_type,
        "avatar": full_user_data.get("avatar_url") or "",
        "status": status,
        "source_created_at": parse_date(full_user_data.get("created_at")),
        "discovery_method": discovery_method,
    }


def _merge_tags(existing_tags: Optional[List[str]], new_tags: List[str]) -> List[str]:
    merged = list(existing_tags or [])
    for tag in new_tags:
        if tag and tag not in merged:
            merged.append(tag)
    return merged


def _build_search_tags(search_query: str = None, add_tags: List[str] = None) -> List[str]:
    tags: List[str] = []
    for tag in [search_query, *(add_tags or [])]:
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def search_users_with_date_splitting(client, search_query: str, logger, job_id) -> List[Dict]:
    start = datetime(2008, 1, 1)
    end = datetime.now()

    logger.info(f"Starting date-split search from {start.date()} to {end.date()}")

    seen: Set[str] = set()
    all_users: List[Dict] = []
    stack: List[Tuple[datetime, datetime]] = [(start, end)]

    while stack:
        if is_cancelled(job_id):
            logger.warning("Task cancelled during date range processing")
            break

        s, e = stack.pop()
        q = _build_date_query(search_query, s, e)
        logger.info(f"Searching: {q}")

        try:
            first = client.search_users(q, per_page=100, page=1)
            total = int(first.get("total_count", 0))
        except Exception as exc:
            logger.error(f"Error searching {s.date()}..{e.date()}: {exc}")
            continue

        logger.info(f"{total} hits for {s.date()}..{e.date()}")

        if total > 1000 and (e - s).days > 1:
            mid = s + timedelta(days=(e - s).days // 2)
            stack.append((mid + timedelta(days=1), e))
            stack.append((s, mid))
            continue

        if total > 1000:
            logger.warning(f"Cannot split further: fetching all results for {s.date()}..{e.date()}")

        items = _pages_from_search(client, q, logger)
        for user_data in items:
            login = user_data.get("login")
            if login and login not in seen:
                seen.add(login)
                all_users.append(user_data)

    logger.info(f"Date splitting complete: found {len(all_users)} unique users")
    return all_users


def create_detailed_user(
    client,
    username: str,
    discovery_method: DiscoveryMethod,
    logger,
) -> Tuple[Optional[User], bool]:
    existing = User.objects.filter(username=username).first()
    if existing:
        return existing, False

    try:
        full_user_data = client.get_user_info(username)
        time.sleep(0.1)

        if not full_user_data or full_user_data.get("id") is None:
            logger.warning(f"Skipping user {username}: invalid GitHub response")
            return None, False

        fields = _build_user_fields(
            full_user_data,
            discovery_method=discovery_method,
            status=UserStatus.UNKNOWN,
        )
        fields.pop("tags", None)

        user = User.objects.create(**fields)
        logger.info(f"Created user {username} (id={user.id})")
        return user, True

    except Exception as exc:
        msg = str(exc).lower()
        if "404" in msg:
            logger.info(f"User {username} not found, skipping")
        elif "constraint" in msg:
            logger.error(f"DB constraint error for {username}: {exc}")
        else:
            logger.error(f"Unexpected error creating user {username}: {exc}")
        return None, False


def create_lightweight_user(
    client,
    username: str,
    discovery_method: DiscoveryMethod,
    logger,
    account_type: AccountType = AccountType.USER,
) -> Tuple[Optional[User], bool]:
    existing = User.objects.filter(username=username).first()

    if existing:
        if existing.source_user_id is None:
            try:
                user_data = client.get_user_info(username)
                existing.source_user_id = (
                    user_data.get("id")
                    if user_data and user_data.get("id")
                    else _placeholder_source_user_id(username)
                )
            except Exception as exc:
                logger.error(f"Failed to fix source_user_id for {username}: {exc}")
                existing.source_user_id = _placeholder_source_user_id(username)

            existing.save(update_fields=["source_user_id"])

        return existing, False

    try:
        user = User.objects.create(
            username=username,
            account_type=account_type,
            url=f"https://github.com/{username}",
            status=UserStatus.UNKNOWN,
            discovery_method=discovery_method,
            source_user_id=_placeholder_source_user_id(username),
        )
        logger.info(f"Created lightweight user record for {username}")
        return user, True
    except Exception as exc:
        logger.error(f"Failed to create lightweight user record for {username}: {exc}")
        return None, False


def create_user_relationship(
    from_user: User,
    to_user: User,
    relationship_type: RelationshipType,
    repo=None,
) -> UserRelationship:
    relationship, _ = UserRelationship.objects.get_or_create(
        from_user=from_user,
        to_user=to_user,
        relationship_type=relationship_type,
        repo=repo,
    )
    return relationship


def create_or_update_discovered_user(
    full_user_data: Dict,
    search_query: str = None,
    set_user_status: str = None,
    add_tags: List[str] = None,
    update_existing: bool = False,
    logger=None,
) -> Tuple[Optional[User], bool, Action]:
    username = full_user_data.get("login")
    if not username:
        return None, False, Action.NO_USERNAME

    existing = User.objects.filter(
        Q(username=username) | Q(source_user_id=full_user_data.get("id"))
    ).first()

    if existing and not update_existing:
        return existing, False, Action.SKIPPED_EXISTING

    tag_names = _build_search_tags(search_query, add_tags)
    fields = _build_user_fields(
        full_user_data,
        discovery_method=DiscoveryMethod.SEARCH,
        status=set_user_status or UserStatus.CONFIRMED,
    )

    if existing:
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.save()
        user = existing
        created = False
        action = Action.UPDATED_EXISTING
    else:
        user = User.objects.create(**fields)
        created = True
        action = Action.CREATED_NEW

    if tag_names:
        tags_objs = [Tag.objects.get_or_create(name=n)[0] for n in tag_names if n]
        user.tags.add(*tags_objs)

    return user, created, action


def _process_user_relationships(
    client,
    user: User,
    fetch_func,
    relationship_type: RelationshipType,
    discovery_method: DiscoveryMethod,
    logger,
    check_cancel,
    reverse: bool = False,
) -> Tuple[int, int]:
    page = 1
    total = 0
    created = 0

    while True:
        if check_cancel():
            logger.warning("Task cancelled during relationship processing")
            return total, created

        try:
            items = fetch_func(user.username, page=page)
            if not items:
                break

            for item in items:
                login = item.get("login")
                if not login:
                    continue

                other, did_create = create_detailed_user(
                    client,
                    login,
                    discovery_method,
                    logger,
                )
                if other is None:
                    other, did_create = create_lightweight_user(
                        client,
                        login,
                        discovery_method,
                        logger,
                    )

                if other is None:
                    continue

                if reverse:
                    create_user_relationship(
                        from_user=other,
                        to_user=user,
                        relationship_type=relationship_type,
                    )
                else:
                    create_user_relationship(
                        from_user=user,
                        to_user=other,
                        relationship_type=relationship_type,
                    )

                total += 1
                if did_create:
                    created += 1

            if len(items) < 100:
                break

            page += 1
            time.sleep(0.1)

        except Exception as exc:
            if "404" in str(exc).lower():
                logger.info(f"No accessible {relationship_type.name.lower()} for {user.username}")
            else:
                logger.error(f"Error fetching {relationship_type.name} page {page} for {user.username}: {exc}")
            break

    return total, created


def process_user_followers(client, user: User, logger, check_cancel) -> Tuple[int, int]:
    count, created = _process_user_relationships(
        client=client,
        user=user,
        fetch_func=client.get_user_followers,
        relationship_type=RelationshipType.FOLLOWER,
        discovery_method=DiscoveryMethod.FOLLOWER,
        logger=logger,
        check_cancel=check_cancel,
        reverse=True,
    )
    logger.info(f"Processed {count} followers for {user.username}")
    return count, created


def process_user_following(client, user: User, logger, check_cancel) -> Tuple[int, int]:
    count, created = _process_user_relationships(
        client=client,
        user=user,
        fetch_func=client.get_user_following,
        relationship_type=RelationshipType.FOLLOWING,
        discovery_method=DiscoveryMethod.FOLLOWING,
        logger=logger,
        check_cancel=check_cancel,
    )
    logger.info(f"Processed {count} following for {user.username}")
    return count, created


def process_organization_members(client, user: User, logger, check_cancel) -> Tuple[int, int]:
    if user.account_type != AccountType.ORGANIZATION:
        logger.info(f"Skipping org members for non-organization {user.username}")
        return 0, 0

    count, created = _process_user_relationships(
        client=client,
        user=user,
        fetch_func=client.get_org_members,
        relationship_type=RelationshipType.ORG_MEMBER,
        discovery_method=DiscoveryMethod.ORG_MEMBER,
        logger=logger,
        check_cancel=check_cancel,
        reverse=True,
    )
    logger.info(f"Processed {count} org members for {user.username}")
    return count, created


def process_discovered_users_batch(
    client,
    user_list: List[Dict],
    search_query: str,
    set_user_status: str,
    add_tags: List[str],
    update_existing: bool,
    logger,
    job_id,
) -> Tuple[int, int, int]:
    saved = 0
    skipped = 0
    errors = 0
    total = len(user_list)

    for idx, user_data in enumerate(user_list, start=1):
        if idx % 10 == 0 and is_cancelled(job_id):
            logger.warning(f"Task cancelled at user {idx}/{total}")
            break

        username = user_data.get("login")
        if not username:
            logger.warning(f"Skipping user {idx}/{total}: no username")
            continue

        if idx % 50 == 0:
            logger.info(f"Processing {idx}/{total}: {username}")

        full_user_data = _fetch_full_user(client, username, logger)
        if full_user_data is None:
            errors += 1
            continue

        try:
            _, _, action = create_or_update_discovered_user(
                full_user_data=full_user_data,
                search_query=search_query,
                set_user_status=set_user_status,
                add_tags=add_tags,
                update_existing=update_existing,
                logger=logger,
            )
        except Exception as exc:
            logger.error(f"Error saving user {username}: {exc}")
            errors += 1
            continue

        if action == Action.NO_USERNAME:
            errors += 1
        elif action == Action.SKIPPED_EXISTING:
            skipped += 1
        elif action in (Action.UPDATED_EXISTING, Action.CREATED_NEW):
            saved += 1
        else:
            logger.warning(f"Unknown action '{action}' for {username}")

        if idx % 10 == 0 or idx == total:
            logger.info(
                f"Progress: {idx}/{total} processed, "
                f"{saved} saved, {skipped} skipped, {errors} errors"
            )

    return saved, skipped, errors