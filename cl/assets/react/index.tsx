import React from 'react';
import ReactDOM from 'react-dom';
import { BrowserRouter, Switch, Route } from 'react-router-dom';
import TagSelect from './TagSelect';
import TagList from './TagList';

function getDataFromReactRoot() {
  const div = document.querySelector('div#react-root');
  if (div && div instanceof HTMLElement) {
    const authStr = div.dataset.authenticated;
    if (!authStr) return {};
    const strParts = authStr.split(':', 2);
    return {
      id: parseInt(strParts[0], 10),
      name: strParts[1],
      editUrl: div.dataset.editUrl,
    };
  } else {
    console.error('Unable to fetch credentials from server. Tags disabled.');
    return {};
  }
}

const App = () => {
  const user = getDataFromReactRoot();
  return (
    <BrowserRouter>
      <Switch>
        <Route path="/dockets">
          <TagSelect {...user} />
        </Route>
        <Route path="/tags">
          <TagList {...user} />
        </Route>
      </Switch>
    </BrowserRouter>
  );
};

ReactDOM.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
  document.getElementById('react-root')
);
