# Epic 1 - Authentication And Users

## Goal

Implement secure user authentication and basic role-based authorization.

## User Stories

- As a user, I can register with email and password.
- As a user, I can log in and log out.
- As a user, I can stay signed in with a secure session.
- As an owner, I can have a role that controls access to sensitive actions.

## Scope

MVP includes email/password auth only. OAuth can be added later.

## Backend Tasks

- Create `users` table:
  - `id`
  - `email`
  - `name`
  - `password_hash`
  - `role`
  - `is_active`
  - `created_at`
  - `updated_at`
- Create auth endpoints:
  - `POST /auth/register`
  - `POST /auth/login`
  - `POST /auth/logout`
  - `GET /auth/me`
- Use secure password hashing.
- Implement JWT access token or secure cookie session.
- Add dependency for current authenticated user.
- Add role guard for `owner`, `developer`, `viewer`.
- Add rate limiting for login attempts.

## Frontend Tasks

- Build login page.
- Build register page.
- Add auth state store.
- Add protected route wrapper.
- Add user menu with logout.
- Show validation errors clearly.

## Database Tasks

- Add Alembic migration for `users`.
- Add unique index on `users.email`.

## Acceptance Criteria

- New users can register and log in.
- Invalid credentials fail with safe error messages.
- Authenticated requests can load `/auth/me`.
- Protected dashboard routes redirect unauthenticated users to login.
- Passwords are never stored in plain text.

## Future Enhancements

- Google OAuth.
- GitHub OAuth.
- Two-factor authentication.
- Configurable session timeout.

