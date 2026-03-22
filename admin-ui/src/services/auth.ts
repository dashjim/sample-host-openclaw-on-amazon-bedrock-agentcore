import { Amplify } from 'aws-amplify';
import {
  signIn as amplifySignIn,
  signOut as amplifySignOut,
  getCurrentUser,
  fetchAuthSession,
  confirmSignIn,
} from 'aws-amplify/auth';
import { config } from '../config';

Amplify.configure({
  Auth: {
    Cognito: {
      userPoolId: config.cognito.userPoolId,
      userPoolClientId: config.cognito.userPoolClientId,
    },
  },
});

export interface SignInResult {
  success: boolean;
  needsNewPassword?: boolean;
  error?: string;
}

export async function signIn(
  email: string,
  password: string
): Promise<SignInResult> {
  try {
    const result = await amplifySignIn({
      username: email,
      password,
      options: { authFlowType: 'USER_PASSWORD_AUTH' },
    });
    if (
      result.nextStep?.signInStep === 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED'
    ) {
      return { success: false, needsNewPassword: true };
    }
    return { success: result.isSignedIn };
  } catch (err: unknown) {
    const message =
      err instanceof Error ? err.message : 'Sign in failed';
    return { success: false, error: message };
  }
}

export async function completeNewPassword(
  newPassword: string
): Promise<SignInResult> {
  try {
    const result = await confirmSignIn({
      challengeResponse: newPassword,
    });
    return { success: result.isSignedIn };
  } catch (err: unknown) {
    const message =
      err instanceof Error ? err.message : 'Password change failed';
    return { success: false, error: message };
  }
}

export async function signOut(): Promise<void> {
  try {
    await amplifySignOut();
  } catch {
    // Ignore errors on sign out
  }
}

export async function getToken(): Promise<string | null> {
  try {
    const session = await fetchAuthSession();
    return session.tokens?.idToken?.toString() || null;
  } catch {
    return null;
  }
}

export async function isAuthenticated(): Promise<boolean> {
  try {
    await getCurrentUser();
    return true;
  } catch {
    return false;
  }
}

export async function getAdminEmail(): Promise<string> {
  try {
    const user = await getCurrentUser();
    return user.signInDetails?.loginId || user.username || 'admin';
  } catch {
    return 'admin';
  }
}
