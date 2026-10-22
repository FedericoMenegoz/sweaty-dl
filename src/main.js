import './style.css';

if (new URL(location.href).searchParams.get('view') === 'bandcamp') {
  import('./bandcamp-controls.js');
} else {
  import('./main-app.js');
}
