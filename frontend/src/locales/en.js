// The source catalogue. Every user-visible string in the SPA is here; a new locale is a copy of
// this file with the values translated, added to CATALOGS in ../i18n.js.
//
// Keys are namespaced by where they appear rather than by their English text, so re-wording a
// button does not orphan every translation of it.
export default {
  'app.name': 'Smart Document Assistant',
  'app.tagline': 'Ask · Cite · Verify',
  'app.skipToContent': 'Skip to the question box',
  'app.main': 'Ask a question about your documents',

  'loading.models': 'Loading models…',
  'loading.ready': 'Ready',

  'auth.signInTitle': 'Sign in to your workspace',
  'auth.signUpTitle': 'Create a workspace',
  'auth.organization': 'Organization',
  'auth.organizationPlaceholder': 'Acme Inc',
  'auth.email': 'Email',
  'auth.emailPlaceholder': 'you@acme.com',
  'auth.password': 'Password',
  'auth.passwordHint': 'At least {min} characters',
  'auth.working': 'Working…',
  'auth.signIn': 'Sign in',
  'auth.signUp': 'Create workspace',
  'auth.haveAccount': 'I already have an account',
  'auth.needAccount': 'Create a new workspace',
  'auth.note':
    'Documents are scoped to your workspace: you can upload, ask and delete inside your own. Administration is reserved to the instance owner.',

  'account.signOut': 'Sign out',
  'account.label': 'Signed in as {email} in {tenant}',

  'composer.label': 'Your question',
  'composer.placeholder': 'Ask a question…',
  'composer.placeholderEmpty': 'Upload a document to get started',
  'composer.attach': 'Attach',
  'composer.attachTitle': 'Upload documents',
  'composer.send': 'Send',
  'composer.charsLeft': '{count} characters left',
  'composer.removeAttached': 'Stop asking about {filename}',

  'mode.label': 'Retrieval mode',
  'mode.dense': 'Dense',
  'mode.hybrid': 'Hybrid',
  'mode.hybrid_rerank': 'Hybrid + Rerank',

  'chat.log': 'Conversation',
  'chat.answerInProgress': 'Answer in progress',
  'chat.retry': 'Retry',
  'chat.thinking_0': 'Thinking…',
  'chat.thinking_1': 'Analyzing sources…',
  'chat.thinking_2': 'Checking citations…',
  'chat.thinking_3': 'Composing answer…',

  'answer.none': 'No answer was generated.',
  'answer.abstainedDefault': "I couldn't find that in the selected documents.",
  'answer.unsupported': 'unsupported',
  'answer.notFoundIn': 'not found in [{ids}]',
  'answer.closestMatch': 'Closest match: {filename} p.{pages}',
  'answer.score': '(score {score})',
  'answer.citation': 'Show source {id}',

  'sources.toggle_one': 'How this was answered · {count} source',
  'sources.toggle_other': 'How this was answered · {count} sources',
  'sources.confidence': '{label} confidence',
  'sources.open': 'Show source {id}, {filename}',
  'sources.page': 'p.{pages}',
  'sources.scoreSuffix': ' · score {score}',

  'documents.title': 'Documents',
  'documents.empty': 'No documents yet',
  'documents.dragHint': 'Drag a doc into chat, or',
  'documents.selectAll': 'Select all',
  'documents.clearAll': 'Clear all',
  'documents.remove': 'Remove {filename}',
  'documents.toggleScope': 'Ask only about {filename}',
  'documents.panel': 'Your documents',

  'history.title': 'History',
  'history.empty': 'No questions yet this session',
  'history.panel': 'Questions asked this session',

  'upload.uploading': 'Uploading… {percent}%',
  'upload.indexing': '{stage}…',
  'upload.indexingDefault': 'Indexing',
  'upload.done': 'Added',
  'upload.reconnecting': 'Reconnecting…',
  'upload.progress': 'Upload progress',

  'tips.attach': 'Attach a PDF or TXT file to begin',
  'tips.ask': 'Ask a question — answers cite the exact source',
  'tips.mode': 'Switch retrieval mode (Hybrid + Rerank) for different tradeoffs',
  'tips.citations': 'Every answer includes citations and a confidence score',

  'common.close': 'Close',

  'error.title': 'Something went wrong',
  'error.body':
    'The page hit an error it could not recover from. Reloading usually clears it; if it keeps happening, the reference below identifies this failure in our logs.',
  'error.reference': 'Reference: {id}',
  'error.reload': 'Reload the page',
}
